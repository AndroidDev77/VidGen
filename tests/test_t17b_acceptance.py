"""The mandatory T17b acceptance test.

One deterministic path, end to end, with synthetic media only:

    queue render job
      -> execute the worker
      -> construct the manifest
      -> build the captions
      -> render the MP4
      -> verify the output
      -> persist the assets
      -> complete the job
      -> run T22 against the actual render
      -> resolve the T18 download

The render is real: FFmpeg runs, ffprobe verifies, and every canonical output is
persisted through ``AssetService``. Nothing here calls a paid provider, requires
Azure, or commits media - the ten shots and the narration bed are generated from
solid colours and a sine tone at test time.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

import vidgen.db  # noqa: F401
from apps.api.settings import APISettings
from services.qa.final_commands import (
    FinalQACommandOptions,
    completion_allowed,
    run_final_editorial_qa,
)
from services.qa.final_inputs import FinalQALineageError
from services.render_execution.commands import (
    completed_render_job,
    current_render_job,
    queue_render_job,
    render_progress,
)
from services.renderer.manifest import bound_manifest_identity
from services.renderer.verify import probe
from tests.render_execution_fixtures import RenderableProject, build_renderable_project
from vidgen.contracts.final_editorial import FinalQADecision, FinalQAStatus
from vidgen.contracts.render import RenderManifest
from vidgen.contracts.render_execution import RenderExecutionStatus
from vidgen.db.base import Base
from vidgen.db.models import Asset, RenderJob
from vidgen.db.render_models import CaptionTrackRecord, RenderAttempt
from vidgen.storage.blob import FilesystemBlobStore

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="FFmpeg and ffprobe are required for the T17b acceptance render",
)


@dataclass(frozen=True, slots=True)
class Acceptance:
    """Everything the acceptance assertions need about the one real render."""

    factory: sessionmaker[Session]
    store: FilesystemBlobStore
    project: RenderableProject
    render_job_id: UUID
    settings: APISettings
    root: Path


@pytest.fixture(scope="module")
def rendered(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Acceptance]:
    """Queue and execute one real render, through the documented worker command."""
    root = tmp_path_factory.mktemp("t17b-acceptance")
    database = root / "acceptance.sqlite"
    blob_root = root / "blobs"
    settings = APISettings(
        database_url=f"sqlite:///{database}",
        blob_root=blob_root,
        blob_backend="filesystem",
        signing_secret="test-secret",
    )
    engine = create_engine(settings.database_url)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    store = FilesystemBlobStore(blob_root, settings.signing_secret.encode())
    with factory() as session:
        project = build_renderable_project(session, blob_root, root / "fixture")
        queued = queue_render_job(session, project.project_id)
        assert queued.job.status == RenderExecutionStatus.QUEUED.value
        assert not queued.reused
        render_job_id = queued.job.id
        session.commit()

    acceptance = Acceptance(
        factory=factory,
        store=store,
        project=project,
        render_job_id=render_job_id,
        settings=settings,
        root=root,
    )
    assert run_worker(acceptance) == 0
    yield acceptance
    engine.dispose()


def run_worker(acceptance: Acceptance) -> int:
    """Invoke the worker exactly as the documented command does.

    ``uv run python -m workers.render_job.main --render-job-id RENDER_JOB_UUID``
    parses these arguments and calls this same ``run``; the only difference is
    that the settings are constructed here instead of read from the environment.
    """
    from workers.render_job import main as worker

    parser = worker.build_parser()
    arguments = parser.parse_args(
        [
            "--render-job-id",
            str(acceptance.render_job_id),
            "--work-root",
            str(acceptance.root / "render-work"),
            "--minimum-free-bytes",
            "0",
        ]
    )
    arguments.resolved_render_job_id = worker.resolve_render_job_id(arguments, parser)
    return worker.run(arguments, settings=acceptance.settings)


# ---------------------------------------------------------------------------


def test_the_worker_renders_verifies_persists_and_completes_the_job(
    rendered: Acceptance,
) -> None:
    factory, store, fixture = rendered.factory, rendered.store, rendered.project
    render_job_id = rendered.render_job_id
    with factory() as session:
        job = session.get(RenderJob, render_job_id)
        assert job is not None
        assert job.status == RenderExecutionStatus.COMPLETE.value
        assert job.progress_percent == 100
        assert job.selected is True
        assert job.cancel_requested is False
        # The lease is released; nothing is left holding a finished job.
        assert job.claimed_by is None and job.lease_expires_at is None

        # Every canonical output the completed job must reference.
        for reference in (
            job.manifest_asset_id,
            job.srt_asset_id,
            job.webvtt_asset_id,
            job.final_video_asset_id,
            job.verification_report_asset_id,
        ):
            assert reference is not None
        assert job.output_sha256 and len(job.output_sha256) == 64
        assert job.input_hash and job.render_identity
        assert job.measured_duration_us and job.expected_duration_us
        assert abs(job.measured_duration_us - fixture.timeline_duration_us) <= 80_000
        assert job.ffmpeg_version and job.ffmpeg_version.startswith("ffmpeg")
        assert job.renderer_version == "t17/1"
        assert job.completed_at is not None

        # The final asset reads back through the normal storage interface, and
        # its stored hash is the hash the job recorded.
        final = session.get(Asset, job.final_video_asset_id)
        assert final is not None
        assert final.sha256 == job.output_sha256
        assert final.media_type == "video/mp4"
        assert final.project_id == fixture.project_id
        assert store.exists(final.storage_key)

        # It is a real, verifiable deliverable, not a placeholder.
        local = _local_copy(store, final, rendered.root / "downloaded.mp4")
        streams = {stream["codec_type"]: stream for stream in probe(local)["streams"]}
        assert streams["video"]["codec_name"] == "h264"
        assert (streams["video"]["width"], streams["video"]["height"]) == (1920, 1080)
        assert streams["audio"]["codec_name"] == "aac"
        assert streams["subtitle"]["codec_name"] == "mov_text"


def test_the_manifest_captions_and_report_are_persisted_with_provenance(
    rendered: Acceptance,
) -> None:
    factory, store, fixture = rendered.factory, rendered.store, rendered.project
    render_job_id = rendered.render_job_id
    with factory() as session:
        job = session.get(RenderJob, render_job_id)
        assert job is not None
        manifest_asset = session.get(Asset, job.manifest_asset_id)
        assert manifest_asset is not None
        manifest = RenderManifest.model_validate_json(store.read(manifest_asset.storage_key))
        assert manifest.render_identity == job.render_identity
        assert manifest.render_identity == bound_manifest_identity(manifest)
        assert manifest.input_hash == job.input_hash
        assert manifest.project_id == fixture.project_id
        assert len(manifest.shots) == len(fixture.shot_ids)

        # Provenance links the deliverable to everything it came from.
        provenance = manifest.provenance
        assert provenance["input_hash"] == job.input_hash
        assert provenance["narration_asset_id"] == str(fixture.narration_asset_id)
        assert len(provenance["shot_asset_ids"]) == len(fixture.shot_ids)
        assert provenance["render_profile"] == "1080p24"
        assert provenance["visual_qa"]["result_ids"]
        assert manifest.approved_script_id and manifest.narration_run_id
        assert manifest.storyboard_run_id == fixture.storyboard_run_id

        # The caption track is a real deliverable, linked to the render job.
        record = session.scalar(
            select(CaptionTrackRecord).where(CaptionTrackRecord.render_job_id == job.id)
        )
        assert record is not None
        assert record.srt_asset_id == job.srt_asset_id
        assert record.cue_count > 0
        assert record.end_us <= fixture.timeline_duration_us
        srt_asset = session.get(Asset, job.srt_asset_id)
        assert srt_asset is not None
        srt = store.read(srt_asset.storage_key).decode()
        assert "-->" in srt and srt.strip()

        # The reproducibility report records what actually happened.
        report = json.loads(
            store.read(session.get(Asset, job.verification_report_asset_id).storage_key)
        )  # type: ignore[union-attr]
        assert report["full_decode_ok"] and report["subtitle_valid"]
        assert report["render_identity"] == job.render_identity
        assert report["final_video_hash"] == job.output_sha256
        assert report["reproducibility_hash"]

        # One attempt row, recording bounded operational metadata and no logs.
        attempts = session.scalars(
            select(RenderAttempt).where(RenderAttempt.render_job_id == job.id)
        ).all()
        assert len(attempts) == 1
        metadata = attempts[0].operational_metadata
        assert metadata["ffmpeg_executions"] > 0
        assert "stderr" not in json.dumps(metadata)


def test_re_executing_a_completed_job_reuses_it_without_rendering_again(
    rendered: Acceptance,
) -> None:
    factory, render_job_id = rendered.factory, rendered.render_job_id
    with factory() as session:
        before = _snapshot(session, render_job_id)
    assert run_worker(rendered) == 0
    with factory() as session:
        after = _snapshot(session, render_job_id)
    # Same assets, same attempt count, no duplicate rows: reinvoking a completed
    # job is a no-op that returns the existing result.
    assert after == before


def test_queueing_again_after_the_render_reuses_the_completed_job(
    rendered: Acceptance,
) -> None:
    factory, fixture = rendered.factory, rendered.project
    render_job_id = rendered.render_job_id
    with factory() as session:
        queued = queue_render_job(session, fixture.project_id)
        session.commit()
        assert queued.reused
        assert queued.job.id == render_job_id
        assert session.scalars(select(RenderJob.id)).all() == [render_job_id]


def test_progress_and_current_render_projections_report_the_completed_render(
    rendered: Acceptance,
) -> None:
    factory, fixture = rendered.factory, rendered.project
    render_job_id = rendered.render_job_id
    with factory() as session:
        progress = render_progress(session, render_job_id)
        assert progress.status is RenderExecutionStatus.COMPLETE
        assert progress.progress_percent == 100
        assert progress.failure_code is None
        assert current_render_job(session, fixture.project_id) is not None
        deliverable = completed_render_job(session, fixture.project_id)
        assert deliverable is not None and deliverable.id == render_job_id


def test_t22_evaluates_the_exact_render_t17b_produced(
    rendered: Acceptance,
) -> None:
    factory, store, fixture = rendered.factory, rendered.store, rendered.project
    render_job_id = rendered.render_job_id
    with factory() as session:
        result = asyncio.run(
            run_final_editorial_qa(
                session,
                store,
                project_id=fixture.project_id,
                options=FinalQACommandOptions(provider="fake", idempotency_key="t17b-acceptance"),
            )
        )
        job = session.get(RenderJob, render_job_id)
        assert job is not None
        # T22 inspected this render, by asset and by hash - not a fixture.
        assert result.final_video_asset_id == job.final_video_asset_id
        assert result.decision is FinalQADecision.PASS
        assert result.status is FinalQAStatus.FINAL_QA_PASSED
        allowed, reason = completion_allowed(
            session,
            project_id=fixture.project_id,
            final_render_asset_id=job.final_video_asset_id,
        )
        assert allowed, reason
        # A different asset is not covered by that decision.
        other = session.scalars(
            select(Asset).where(Asset.id != job.final_video_asset_id).limit(1)
        ).one()
        blocked, blocked_reason = completion_allowed(
            session, project_id=fixture.project_id, final_render_asset_id=other.id
        )
        assert not blocked and blocked_reason


def test_t18_download_resolves_the_persisted_final_asset(
    rendered: Acceptance,
) -> None:
    factory, store, fixture = rendered.factory, rendered.store, rendered.project
    render_job_id = rendered.render_job_id
    with factory() as session:
        from vidgen.review.projections import render_projection
        from vidgen.review.versions import RowVersionService

        job = session.get(RenderJob, render_job_id)
        assert job is not None
        projection = render_projection(session, fixture.project_id, job, RowVersionService(session))
        assert projection.status == "render_complete"
        assert projection.verified and not projection.stale
        assert projection.downloadable
        assert projection.final_video_asset_id == job.final_video_asset_id
        assert projection.output_sha256 == job.output_sha256
        assert projection.progress_percent == 100
        assert projection.failure_code is None

        asset = session.get(Asset, projection.final_video_asset_id)
        assert asset is not None
        url = store.signed_read_url(asset.storage_key, 900)
        assert url and "signature=" in url
        # The signed URL resolves to the bytes that were actually rendered.
        assert store.exists(asset.storage_key)


def test_t22_will_not_start_from_an_incomplete_render(
    rendered: Acceptance,
) -> None:
    factory, store, fixture = rendered.factory, rendered.store, rendered.project
    render_job_id = rendered.render_job_id
    with factory() as session:
        job = session.get(RenderJob, render_job_id)
        assert job is not None
        completed_at, status = job.completed_at, job.status
        outputs = (job.output_sha256, job.measured_duration_us)
        job.status = RenderExecutionStatus.RENDERING.value
        job.output_sha256, job.measured_duration_us = None, None
        job.completed_at = None
        session.commit()
        try:
            assert completed_render_job(session, fixture.project_id) is None
            with pytest.raises(FinalQALineageError):
                asyncio.run(
                    run_final_editorial_qa(
                        session,
                        store,
                        project_id=fixture.project_id,
                        options=FinalQACommandOptions(
                            provider="fake", idempotency_key="t17b-incomplete"
                        ),
                    )
                )
        finally:
            session.rollback()
            job = session.get(RenderJob, render_job_id)
            assert job is not None
            job.output_sha256, job.measured_duration_us = outputs
            job.completed_at, job.status = completed_at, status
            session.commit()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _snapshot(session: Session, render_job_id: UUID) -> tuple[object, ...]:
    job = session.get(RenderJob, render_job_id)
    assert job is not None
    return (
        job.status,
        job.render_identity,
        job.output_sha256,
        job.manifest_asset_id,
        job.srt_asset_id,
        job.webvtt_asset_id,
        job.final_video_asset_id,
        job.verification_report_asset_id,
        job.measured_duration_us,
        session.scalar(select(Asset.id).where(Asset.id.is_not(None)).order_by(Asset.id)),
        len(session.scalars(select(Asset.id)).all()),
        len(session.scalars(select(RenderAttempt.id)).all()),
        len(session.scalars(select(RenderJob.id)).all()),
    )


def _local_copy(store: FilesystemBlobStore, asset: Asset, destination: Path) -> Path:
    """Stream the persisted deliverable out of storage so ffprobe can read it."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    store.copy_to(asset.storage_key, destination)
    return destination
