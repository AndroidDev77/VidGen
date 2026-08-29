"""T17b: authoritative input selection, manifest construction, claims and worker.

The expensive part of these tests is the fixture, not the assertions: building a
renderable ten-shot project runs FFmpeg once per shot. It is therefore built
once per module into a template database, and every test starts from a copy of
that template so the tests stay independent without paying for it ten times.

The full render is exercised in ``tests/test_t17b_acceptance.py``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import vidgen.db.models
import vidgen.db.script_models
import vidgen.db.upload_models  # noqa: F401
from services.render_execution import commands as render_commands
from services.render_execution.claims import (
    RenderClaimError,
    checkpoint,
    claim_render_job,
    heartbeat,
    progress_of,
    request_cancellation,
    require_lease,
)
from services.render_execution.ffmpeg import (
    CancellableCommandExecutor,
    RenderCancelled,
    RenderTimeout,
)
from services.render_execution.inputs import render_settings_for, resolve_render_inputs
from services.render_execution.manifest_builder import build_captions, build_manifest
from services.renderer.manifest import bound_manifest_identity, canonical_json
from services.renderer.selection import RenderLineageError
from tests.render_execution_fixtures import RenderableProject, build_renderable_project
from vidgen.contracts.render import RenderInputReference
from vidgen.contracts.render_execution import RenderExecutionStatus
from vidgen.db.animation_models import AnimationGeneratedVideo
from vidgen.db.base import Base
from vidgen.db.models import Asset, Project, RenderJob
from vidgen.db.repair_models import RepairRun
from vidgen.db.script_models import Script
from vidgen.db.visual_qa_models import VisualQARun
from vidgen.db.visual_qa_repository import VisualQARepository
from vidgen.storage.blob import FilesystemBlobStore

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="FFmpeg and ffprobe are required to build the render fixture",
)


@pytest.fixture(scope="module")
def template(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path, RenderableProject]:
    """One renderable ten-shot project, built once and copied per test."""
    root = tmp_path_factory.mktemp("t17b-template")
    database = root / "template.sqlite"
    engine = create_engine(f"sqlite:///{database}")
    Base.metadata.create_all(engine)
    blob_root = root / "blobs"
    with Session(engine, expire_on_commit=False) as session:
        fixture = build_renderable_project(session, blob_root, root / "work")
    engine.dispose()
    return database, blob_root, fixture


@pytest.fixture
def project(
    template: tuple[Path, Path, RenderableProject], tmp_path: Path
) -> Iterator[tuple[Session, FilesystemBlobStore, RenderableProject]]:
    database, blob_root, fixture = template
    copy = tmp_path / "test.sqlite"
    shutil.copyfile(database, copy)
    engine = create_engine(f"sqlite:///{copy}")
    store = FilesystemBlobStore(blob_root, b"test-secret")
    with Session(engine, expire_on_commit=False) as session:
        yield session, store, fixture
    engine.dispose()


def queue(session: Session, fixture: RenderableProject, **kwargs: object) -> RenderJob:
    queued = render_commands.queue_render_job(session, fixture.project_id, **kwargs)  # type: ignore[arg-type]
    session.commit()
    return queued.job


# ---------------------------------------------------------------------------
# Authoritative input selection
# ---------------------------------------------------------------------------


def test_authoritative_inputs_resolve_to_a_stable_input_identity(
    project: tuple[Session, FilesystemBlobStore, RenderableProject],
) -> None:
    session, _store, fixture = project
    job = queue(session, fixture)
    first = resolve_render_inputs(session, job=job)
    second = resolve_render_inputs(session, job=job)
    assert first.input_hash == second.input_hash == job.input_hash
    assert first.contract.shot_count == len(fixture.shot_ids)
    assert first.contract.target_duration_us == fixture.timeline_duration_us
    assert first.contract.narration_asset_id == fixture.narration_asset_id
    assert first.contract.aspect_ratio == "16:9"
    # Identity must not depend on when the inputs were resolved.
    assert first.contract.model_dump(
        mode="json", exclude={"resolved_at"}
    ) == second.contract.model_dump(mode="json", exclude={"resolved_at"})


def test_queueing_twice_reuses_one_render_job(
    project: tuple[Session, FilesystemBlobStore, RenderableProject],
) -> None:
    session, _store, fixture = project
    first = render_commands.queue_render_job(session, fixture.project_id)
    session.commit()
    second = render_commands.queue_render_job(session, fixture.project_id)
    session.commit()
    assert second.reused
    assert second.job.id == first.job.id
    assert session.scalars(select(RenderJob.id)).all() == [first.job.id]


def test_a_material_input_change_produces_a_new_input_identity(
    project: tuple[Session, FilesystemBlobStore, RenderableProject],
) -> None:
    session, _store, fixture = project
    job = queue(session, fixture)
    before = job.input_hash
    # Selecting a different subtitle mode is a materially different deliverable.
    other = RenderJob(
        id=uuid4(),
        project_id=fixture.project_id,
        status=RenderExecutionStatus.QUEUED.value,
        video_profile={"name": "1080p30"},
        caption_profile={"subtitle_mode": "selectable", "language": "en"},
    )
    assert resolve_render_inputs(session, job=other).input_hash != before


def test_a_cross_project_shot_asset_is_rejected(
    project: tuple[Session, FilesystemBlobStore, RenderableProject],
) -> None:
    session, _store, fixture = project
    stranger = Project(name="other", visual_style="flat", settings={})
    session.add(stranger)
    session.flush()
    video = session.scalars(select(AnimationGeneratedVideo).limit(1)).one()
    asset = session.get(Asset, video.canonical_asset_id)
    assert asset is not None
    asset.project_id = stranger.id
    session.flush()
    with pytest.raises(RenderLineageError) as error:
        render_commands.queue_render_job(session, fixture.project_id)
    assert error.value.code == "video_asset_mismatch"


def test_a_stale_clip_duration_is_rejected(
    project: tuple[Session, FilesystemBlobStore, RenderableProject],
) -> None:
    session, _store, fixture = project
    video = session.scalars(select(AnimationGeneratedVideo).limit(1)).one()
    video.canonical_duration = video.canonical_duration + 0.5
    session.flush()
    with pytest.raises(RenderLineageError) as error:
        render_commands.queue_render_job(session, fixture.project_id)
    assert error.value.code == "stale_clip_duration"


def test_an_unapproved_script_is_rejected(
    project: tuple[Session, FilesystemBlobStore, RenderableProject],
) -> None:
    session, _store, fixture = project
    script = session.scalars(
        select(Script).where(Script.project_id == fixture.project_id, Script.selected.is_(True))
    ).one()
    script.status = "draft"
    session.flush()
    with pytest.raises(RenderLineageError) as error:
        render_commands.queue_render_job(session, fixture.project_id)
    assert error.value.code == "script_not_approved"


def test_an_active_repair_run_blocks_the_render(
    project: tuple[Session, FilesystemBlobStore, RenderableProject],
) -> None:
    session, _store, fixture = project
    _add_repair_run(session, fixture, state="REPAIRING")
    with pytest.raises(RenderLineageError) as error:
        render_commands.queue_render_job(session, fixture.project_id)
    assert error.value.code == "active_repair_run"


def test_a_locked_repair_output_must_be_the_rendered_clip(
    project: tuple[Session, FilesystemBlobStore, RenderableProject],
) -> None:
    session, _store, fixture = project
    run = _add_repair_run(session, fixture, state="LOCKED")
    # A LOCKED repair whose selected output *is* the selected clip renders.
    resolved = resolve_render_inputs(session, job=_probe(fixture))
    assert run.id in resolved.repair_run_ids
    # Point the locked repair at a different asset: the render would then be
    # assembling the superseded original, which is exactly what must not happen.
    stranger = session.scalars(
        select(Asset).where(Asset.kind == "canonical_shot_video").limit(1)
    ).one()
    run.selected_asset_id = uuid4() if stranger.id == run.selected_asset_id else stranger.id
    session.flush()
    with pytest.raises(RenderLineageError) as error:
        resolve_render_inputs(session, job=_probe(fixture))
    assert error.value.code == "stale_shot_selection"


def test_unsupported_render_settings_are_rejected(
    project: tuple[Session, FilesystemBlobStore, RenderableProject],
) -> None:
    _session, _store, fixture = project
    job = _probe(fixture)
    job.video_profile = {"name": "4320p120"}
    with pytest.raises(RenderLineageError) as error:
        render_settings_for(job)
    assert error.value.code == "unsupported_render_settings"


# ---------------------------------------------------------------------------
# Captions and the production manifest
# ---------------------------------------------------------------------------


def test_the_caption_track_uses_approved_words_and_stays_inside_the_timeline(
    project: tuple[Session, FilesystemBlobStore, RenderableProject],
) -> None:
    session, _store, fixture = project
    resolved = resolve_render_inputs(session, job=_probe(fixture))
    captions = build_captions(resolved)
    track = captions.track
    assert track.duration_us == fixture.timeline_duration_us
    assert track.cues[0].start_us >= 0
    assert track.cues[-1].end_us <= fixture.timeline_duration_us
    for previous, cue in zip(track.cues, track.cues[1:], strict=False):
        assert cue.start_us >= previous.end_us
        assert cue.end_us > cue.start_us
    # The cues carry the approved words, in order, at their measured times.
    spoken = " ".join(word.text for word in resolved.words)
    rendered = " ".join(line for cue in track.cues for line in cue.lines)
    assert rendered.replace("  ", " ") == spoken.replace("  ", " ")
    # Identical inputs produce an identical track.
    assert (
        build_captions(resolved).validation.caption_identity == captions.validation.caption_identity
    )


def test_the_production_manifest_binds_its_identity_and_is_deterministic(
    project: tuple[Session, FilesystemBlobStore, RenderableProject],
) -> None:
    session, _store, fixture = project
    resolved = resolve_render_inputs(session, job=queue(session, fixture))
    captions = build_captions(resolved)
    references = _caption_references(captions)
    first = build_manifest(resolved, captions, references)
    second = build_manifest(resolved, captions, references)

    assert first.render_identity == bound_manifest_identity(first)
    assert first.render_identity == second.render_identity
    # created_at and manifest_id are envelope metadata, so the canonical bodies
    # of two builds over the same inputs are byte-identical apart from them.
    assert first.manifest_id == second.manifest_id
    assert canonical_json(
        first.model_copy(update={"created_at": second.created_at})
    ) == canonical_json(second)

    assert first.project_id == fixture.project_id
    assert first.narration_duration_us == fixture.timeline_duration_us
    assert [shot.sequence for shot in first.shots] == list(range(len(fixture.shot_ids)))
    assert first.shots[0].global_start_us == 0
    assert first.shots[-1].global_end_us == fixture.timeline_duration_us
    # Exact integer timing, never accumulated float drift.
    for shot in first.shots:
        assert shot.global_end_us - shot.global_start_us == shot.exact_usable_duration_us
        assert shot.trim_end_us - shot.trim_start_us == shot.exact_usable_duration_us
    assert sum(entry.role == "narration" for entry in first.audio_entries) == 1
    assert first.input_hash == resolved.input_hash
    assert first.provenance["input_hash"] == resolved.input_hash
    assert first.provenance["shot_asset_ids"]
    # No signed URL, path or credential is part of canonical identity.
    body = canonical_json(first).decode()
    assert "http" not in body and "/tmp" not in body


def test_the_manifest_refuses_caption_assets_that_do_not_match_the_track(
    project: tuple[Session, FilesystemBlobStore, RenderableProject],
) -> None:
    session, _store, fixture = project
    resolved = resolve_render_inputs(session, job=queue(session, fixture))
    captions = build_captions(resolved)
    references = _caption_references(captions)
    references["caption_srt"] = references["caption_srt"].model_copy(update={"sha256": "b" * 64})
    with pytest.raises(RenderLineageError) as error:
        build_manifest(resolved, captions, references)
    assert error.value.code == "caption_asset_mismatch"


# ---------------------------------------------------------------------------
# Claims, leases and checkpoints
# ---------------------------------------------------------------------------


def test_only_one_worker_can_hold_a_render_job(
    project: tuple[Session, FilesystemBlobStore, RenderableProject],
) -> None:
    session, _store, fixture = project
    job = queue(session, fixture)
    claim = claim_render_job(
        session, render_job_id=job.id, worker_id="worker-a", lease_seconds=300, max_attempts=3
    )
    session.commit()
    assert claim.worker_id == "worker-a"
    with pytest.raises(RenderClaimError) as error:
        claim_render_job(
            session, render_job_id=job.id, worker_id="worker-b", lease_seconds=300, max_attempts=3
        )
    assert error.value.code == "render_job_leased"
    # The holder may re-enter its own claim: a duplicate invocation resumes.
    again = claim_render_job(
        session, render_job_id=job.id, worker_id="worker-a", lease_seconds=300, max_attempts=3
    )
    assert again.attempt == claim.attempt + 1


def test_a_stale_lease_is_reclaimed_and_a_heartbeat_extends_a_live_one(
    project: tuple[Session, FilesystemBlobStore, RenderableProject],
) -> None:
    session, _store, fixture = project
    job = queue(session, fixture)
    past = datetime.now(UTC) - timedelta(hours=1)
    claim = claim_render_job(
        session,
        render_job_id=job.id,
        worker_id="worker-a",
        lease_seconds=300,
        max_attempts=5,
        now=past,
    )
    session.commit()
    # The first worker died an hour ago; the second may take over.
    recovered = claim_render_job(
        session, render_job_id=job.id, worker_id="worker-b", lease_seconds=300, max_attempts=5
    )
    session.commit()
    assert recovered.worker_id == "worker-b"
    assert recovered.attempt == claim.attempt + 1

    # The displaced worker's heartbeat fails rather than silently continuing.
    with pytest.raises(RenderClaimError) as error:
        heartbeat(session, claim=claim, lease_seconds=300)
    assert error.value.code == "render_lease_lost"
    session.rollback()

    before = require_lease(session, recovered).lease_expires_at
    extended = heartbeat(session, claim=recovered, lease_seconds=900, progress_percent=42)
    session.commit()
    assert before is not None and extended > before.replace(tzinfo=UTC)
    assert require_lease(session, recovered).progress_percent == 42


def test_attempts_are_bounded(
    project: tuple[Session, FilesystemBlobStore, RenderableProject],
) -> None:
    session, _store, fixture = project
    job = queue(session, fixture)
    for _ in range(2):
        claim_render_job(
            session, render_job_id=job.id, worker_id="worker", lease_seconds=1, max_attempts=2
        )
        session.commit()
    with pytest.raises(RenderClaimError) as error:
        claim_render_job(
            session, render_job_id=job.id, worker_id="worker", lease_seconds=1, max_attempts=2
        )
    assert error.value.code == "render_attempts_exhausted"


def test_checkpoints_are_durable_and_projected(
    project: tuple[Session, FilesystemBlobStore, RenderableProject],
) -> None:
    session, _store, fixture = project
    job = queue(session, fixture)
    claim = claim_render_job(
        session, render_job_id=job.id, worker_id="worker", lease_seconds=300, max_attempts=3
    )
    record = checkpoint(
        session,
        claim=claim,
        status=RenderExecutionStatus.RENDERING,
        phase="ffmpeg",
        progress_percent=40,
    )
    session.commit()
    session.expire_all()
    stored = session.get(RenderJob, job.id)
    assert stored is not None
    assert stored.status == RenderExecutionStatus.RENDERING.value
    assert stored.checkpoint == "ffmpeg"
    assert stored.progress_percent == 40
    assert record.status is RenderExecutionStatus.RENDERING

    progress = progress_of(stored)
    assert progress.status is RenderExecutionStatus.RENDERING
    assert progress.progress_percent == 40
    assert progress.claimed_by == "worker"


def test_cancellation_is_requested_and_refuses_a_new_claim(
    project: tuple[Session, FilesystemBlobStore, RenderableProject],
) -> None:
    session, _store, fixture = project
    job = queue(session, fixture)
    assert request_cancellation(session, job.id)
    session.commit()
    with pytest.raises(RenderClaimError) as error:
        claim_render_job(
            session, render_job_id=job.id, worker_id="worker", lease_seconds=300, max_attempts=3
        )
    assert error.value.code == "render_job_cancelled"


def test_a_completed_job_is_never_claimed(
    project: tuple[Session, FilesystemBlobStore, RenderableProject],
) -> None:
    session, _store, fixture = project
    job = queue(session, fixture)
    _force_complete(session, job)
    with pytest.raises(RenderClaimError) as error:
        claim_render_job(
            session, render_job_id=job.id, worker_id="worker", lease_seconds=300, max_attempts=3
        )
    assert error.value.code == "render_job_complete"


# ---------------------------------------------------------------------------
# Database constraints
# ---------------------------------------------------------------------------


def test_a_completed_render_job_must_reference_its_outputs(
    project: tuple[Session, FilesystemBlobStore, RenderableProject],
) -> None:
    session, _store, fixture = project
    job = queue(session, fixture)
    job.status = RenderExecutionStatus.COMPLETE.value
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


@pytest.mark.parametrize(
    ("field", "value"), [("progress_percent", 101), ("progress_percent", -1), ("attempt_count", -1)]
)
def test_render_job_execution_state_is_range_checked(
    project: tuple[Session, FilesystemBlobStore, RenderableProject], field: str, value: int
) -> None:
    session, _store, fixture = project
    job = queue(session, fixture)
    setattr(job, field, value)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


# ---------------------------------------------------------------------------
# The FFmpeg execution wrapper
# ---------------------------------------------------------------------------


def test_ffmpeg_diagnostics_are_bounded_and_failures_are_raised() -> None:
    executor = CancellableCommandExecutor(timeout_seconds=30, output_limit=64)
    noise = "x" * 5000
    with pytest.raises(RuntimeError) as error:
        executor.run(["python", "-c", f"import sys; sys.stderr.write({noise!r}); sys.exit(3)"], "p")
    assert len(executor.last_stderr_tail) <= 64
    assert "exit 3" in str(error.value)
    assert len(str(error.value)) < 500


def test_ffmpeg_execution_honours_a_timeout() -> None:
    executor = CancellableCommandExecutor(timeout_seconds=1, poll_interval_seconds=0.05)
    with pytest.raises(RenderTimeout):
        executor.run(["python", "-c", "import time; time.sleep(30)"], "encode")


def test_ffmpeg_execution_terminates_on_cancellation() -> None:
    flag = {"cancelled": False}

    def cancelled() -> bool:
        flag["cancelled"] = True
        return True

    executor = CancellableCommandExecutor(cancelled=cancelled)
    with pytest.raises(RenderCancelled):
        executor.run(["python", "-c", "import time; time.sleep(30)"], "encode")
    assert flag["cancelled"]


def test_the_executor_never_builds_a_shell_command() -> None:
    executor = CancellableCommandExecutor()
    with pytest.raises(TypeError):
        executor.run("ffmpeg -i in.mp4 out.mp4", "encode")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The worker entry point
# ---------------------------------------------------------------------------


def test_the_worker_requires_a_render_job_id() -> None:
    from workers.render_job.main import main

    with pytest.raises(SystemExit) as exit_info:
        main([])
    assert exit_info.value.code == 2


def test_the_worker_reads_its_render_job_id_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from workers.render_job.main import build_parser, resolve_render_job_id

    parser = build_parser()
    identifier = uuid4()
    monkeypatch.setenv("VIDGEN_RENDER_JOB_ID", str(identifier))
    assert resolve_render_job_id(parser.parse_args(["--from-env"]), parser) == identifier
    monkeypatch.setenv("VIDGEN_RENDER_JOB_ID", "")
    with pytest.raises(SystemExit):
        resolve_render_job_id(parser.parse_args(["--from-env"]), parser)


def test_the_worker_maps_execution_status_onto_exit_codes() -> None:
    from vidgen.contracts.render_execution import RenderExecutionResult
    from workers.render_job.main import EXIT_CANCELLED, EXIT_FAILED, EXIT_OK, worker_result

    project_id, job_id = uuid4(), uuid4()
    digest = "a" * 64
    complete = RenderExecutionResult(
        render_job_id=job_id,
        project_id=project_id,
        status=RenderExecutionStatus.COMPLETE,
        reused=True,
        render_identity=digest,
        output_sha256=digest,
        manifest_asset_id=uuid4(),
        caption_srt_asset_id=uuid4(),
        final_video_asset_id=uuid4(),
        verification_report_asset_id=uuid4(),
    )
    assert worker_result(complete).exit_code == EXIT_OK
    assert worker_result(complete).reused

    failed = RenderExecutionResult(
        render_job_id=job_id,
        project_id=project_id,
        status=RenderExecutionStatus.FAILED,
        failure={  # type: ignore[arg-type]
            "classification": "execution",
            "code": "render_execution_failed",
            "message": "boom",
            "retryable": True,
        },
    )
    assert worker_result(failed).exit_code == EXIT_FAILED
    assert worker_result(failed).failure_code == "render_execution_failed"

    cancelled = failed.model_copy(update={"status": RenderExecutionStatus.CANCELLED})
    assert worker_result(cancelled).exit_code == EXIT_CANCELLED


def test_the_worker_prints_one_compact_json_record_and_no_media() -> None:
    from vidgen.contracts.render_execution import RenderExecutionResult
    from workers.render_job.main import worker_result

    record = worker_result(
        RenderExecutionResult(
            render_job_id=uuid4(),
            project_id=uuid4(),
            status=RenderExecutionStatus.COMPLETE,
            render_identity="c" * 64,
            output_sha256="c" * 64,
            manifest_asset_id=uuid4(),
            caption_srt_asset_id=uuid4(),
            final_video_asset_id=uuid4(),
            verification_report_asset_id=uuid4(),
        )
    )
    payload = json.loads(record.model_dump_json())
    assert set(payload) <= {
        "schema_version",
        "render_job_id",
        "status",
        "reused",
        "exit_code",
        "final_video_asset_id",
        "output_sha256",
        "measured_duration_us",
        "failure_code",
        "failure_classification",
    }


def test_the_worker_module_is_runnable() -> None:
    result = subprocess.run(
        ["python", "-m", "workers.render_job.main", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "--render-job-id" in result.stdout
    assert "--from-env" in result.stdout


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _probe(fixture: RenderableProject) -> RenderJob:
    return RenderJob(
        id=uuid4(),
        project_id=fixture.project_id,
        status=RenderExecutionStatus.QUEUED.value,
        video_profile={"name": "1080p24"},
        caption_profile={"subtitle_mode": "selectable", "language": "en"},
    )


def _caption_references(captions: object) -> dict[str, RenderInputReference]:
    import hashlib

    payloads = captions.payloads()  # type: ignore[attr-defined]
    media = {
        "caption_srt": "application/x-subrip",
        "caption_webvtt": "text/vtt",
        "caption_ass": "text/x-ssa",
    }
    return {
        role: RenderInputReference(
            asset_id=uuid4(),
            sha256=hashlib.sha256(payloads[role]).hexdigest(),
            media_type=media[role],
            role=role,
        )
        for role in ("caption_srt", "caption_webvtt")
    }


def _add_repair_run(session: Session, fixture: RenderableProject, *, state: str) -> RepairRun:
    """One T21 repair run over the project's first shot, in the given state."""
    video = session.scalars(
        select(AnimationGeneratedVideo)
        .where(AnimationGeneratedVideo.shot_id == fixture.shot_ids[0])
        .limit(1)
    ).one()
    qa_run = session.scalars(
        select(VisualQARun).where(
            VisualQARun.shot_id == fixture.shot_ids[0], VisualQARun.target_type == "video"
        )
    ).first()
    assert qa_run is not None
    result = VisualQARepository(session).canonical_result(qa_run.id)
    assert result is not None
    run = RepairRun(
        project_id=fixture.project_id,
        shot_id=fixture.shot_ids[0],
        root_animation_attempt_id=video.id,
        triggering_qa_result_id=result.id,
        policy_version="t21/1",
        policy={},
        classifier_version="t21/1",
        planner_version="t21/1",
        input_hash=f"{7:064x}",
        idempotency_key=f"t21-{state.lower()}",
        state=state,
        # A LOCKED run must name the attempt it selected and the QA result that
        # cleared it; the table's own constraint enforces that.
        selected_attempt_id=video.id if state == "LOCKED" else None,
        final_qa_result_id=result.id if state == "LOCKED" else None,
        selected_asset_id=video.canonical_asset_id if state == "LOCKED" else None,
    )
    session.add(run)
    session.flush()
    return run


def _force_complete(session: Session, job: RenderJob) -> None:
    """Mark a job complete with the outputs the table's constraints require."""
    asset = session.scalars(select(Asset).limit(1)).one()
    job.status = RenderExecutionStatus.COMPLETE.value
    job.manifest_asset_id = asset.id
    job.srt_asset_id = asset.id
    job.webvtt_asset_id = asset.id
    job.final_video_asset_id = asset.id
    job.verification_report_asset_id = asset.id
    job.output_sha256 = asset.sha256
    job.measured_duration_us = 1_000
    job.completed_at = datetime.now(UTC)
    session.commit()
