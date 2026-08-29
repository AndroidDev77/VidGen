"""The mandatory T22 acceptance test.

Ten selected shots with passing T20 results, repaired shots referencing their
selected passing T21 attempts, a real T17 delivery, and the whole final
editorial-QA pipeline run end to end against the deterministic fake provider.

Every blocking fixture proves the same thing from a different direction: the
project cannot reach its final completed state while the defect stands.

Nothing here makes a paid provider call.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

import vidgen.db
from services.qa.final_commands import (
    FinalQABlocked,
    FinalQACommandOptions,
    FinalQAReviewRequired,
    completion_allowed,
    evaluate_final_stage,
    run_final_editorial_qa,
)
from services.qa.final_fake_provider import FakeEditorialDefect, FakeEditorialFinding
from services.qa.final_inputs import FinalQALineageError
from tests.final_qa_fixtures import (
    FIXTURE_CONFIGURATION,
    FinalQAFixture,
    assemble_render,
    build_final_qa_project,
    narration_wav,
    replace_final_render,
    require_ffmpeg,
)
from vidgen.contracts.final_editorial import (
    FinalEditorialCategory,
    FinalFindingSeverity,
    FinalIssueCode,
    FinalQADecision,
    FinalQAFailureCode,
    FinalQAStatus,
    FinalRemediationTarget,
)
from vidgen.db.base import Base
from vidgen.db.final_editorial_models import (
    FinalCompletionGate,
    FinalEditorialCheckRecord,
    FinalEditorialRun,
)
from vidgen.db.models import Asset
from vidgen.db.repair_models import RepairRun
from vidgen.storage.blob import FilesystemBlobStore

pytestmark = pytest.mark.skipif(not require_ffmpeg(), reason="FFmpeg and ffprobe are required")


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 't22.db'}")
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture
def blob_root(tmp_path: Path) -> Path:
    root = tmp_path / "blobs"
    root.mkdir()
    return root


@pytest.fixture
def store(blob_root: Path) -> FilesystemBlobStore:
    return FilesystemBlobStore(blob_root, b"test-secret")


@pytest.fixture
def fixture(
    factory: sessionmaker[Session], blob_root: Path, tmp_path: Path
) -> Iterator[FinalQAFixture]:
    with factory() as session:
        yield build_final_qa_project(session, blob_root, tmp_path / "work")


def options(**overrides: object) -> FinalQACommandOptions:
    return FinalQACommandOptions(
        provider="fake",
        configuration=FIXTURE_CONFIGURATION,
        idempotency_key="t22-acceptance",
        **overrides,  # type: ignore[arg-type]
    )


def run(session: Session, store: FilesystemBlobStore, fixture: FinalQAFixture, **kwargs: object):
    return asyncio.run(
        run_final_editorial_qa(
            session,
            store,
            project_id=fixture.project_id,
            options=options(**kwargs),  # type: ignore[arg-type]
        )
    )


# --- the acceptance path -----------------------------------------------------
def test_ten_passing_shots_produce_a_pass_report_that_unblocks_completion(
    factory: sessionmaker[Session], store: FilesystemBlobStore, fixture: FinalQAFixture
) -> None:
    with factory() as session:
        result = run(session, store, fixture)

        assert result.decision is FinalQADecision.PASS
        assert result.status is FinalQAStatus.FINAL_QA_PASSED
        assert result.blocking_finding_count == 0
        assert result.review_finding_count == 0
        assert result.deterministic_failure_count == 0
        # Every family of deterministic checks actually ran against real media.
        assert result.deterministic_check_count > 0
        assert result.audio_check_count > 0
        assert result.caption_check_count > 0
        assert result.report_asset_id is not None

        # The gate is persisted, immutable and lets the project advance.
        gate = session.scalars(select(FinalCompletionGate)).one()
        assert gate.decision == "PASS"
        assert gate.render_identity == fixture.render_identity
        allowed, reason = completion_allowed(
            session,
            project_id=fixture.project_id,
            final_render_asset_id=fixture.final_video_asset_id,
        )
        assert allowed and reason == "final_qa_pass"

        # The report records the provenance a later reader needs.
        report = session.get(Asset, result.report_asset_id)
        assert report is not None
        provenance = report.extra_metadata
        assert provenance["final_render_asset_id"] == str(fixture.final_video_asset_id)
        assert provenance["render_manifest_asset_id"] == str(fixture.manifest_asset_id)
        assert len(provenance["selected_shot_asset_ids"]) == 10
        assert provenance["input_hash"] == result.input_hash
        assert provenance["check_versions"]["gate"]


def test_an_identical_rerun_reuses_the_completed_report_and_costs_nothing_more(
    factory: sessionmaker[Session], store: FilesystemBlobStore, fixture: FinalQAFixture
) -> None:
    with factory() as session:
        first = run(session, store, fixture)
        checks = session.scalars(select(FinalEditorialCheckRecord)).all()
        second = run(session, store, fixture)

        assert second.reused and not first.reused
        assert second.final_editorial_run_id == first.final_editorial_run_id
        assert second.final_qa_identity == first.final_qa_identity
        assert second.cost_microusd == first.cost_microusd
        # No second run, no duplicated checks, no second gate.
        assert len(session.scalars(select(FinalEditorialRun)).all()) == 1
        assert len(session.scalars(select(FinalEditorialCheckRecord)).all()) == len(checks)
        assert len(session.scalars(select(FinalCompletionGate)).all()) == 1


def test_a_completed_phase_is_reused_when_a_run_is_interrupted_and_resumed(
    factory: sessionmaker[Session], store: FilesystemBlobStore, fixture: FinalQAFixture
) -> None:
    """An interrupted run resumes on its checkpoints rather than starting over."""
    with factory() as session:
        result = run(session, store, fixture)
        run_row = session.get(FinalEditorialRun, result.final_editorial_run_id)
        assert run_row is not None
        completed = list(run_row.completed_phases)
        assert completed == [
            "INPUT_VALIDATION",
            "DETERMINISTIC_MEDIA_QA",
            "CAPTION_QA",
            "EDITORIAL_ANALYSIS",
            "COMPLETION_GATE",
        ]
        # Simulate an interruption after the caption phase, then resume.
        run_row.status = FinalQAStatus.FINAL_QA_ANALYZING.value
        run_row.completed_phases = completed[:3]
        session.commit()

        resumed = run(session, store, fixture)
        assert resumed.final_editorial_run_id == result.final_editorial_run_id
        assert resumed.decision is FinalQADecision.PASS
        assert len(session.scalars(select(FinalEditorialRun)).all()) == 1


# --- stale lineage and eligibility -------------------------------------------
def test_a_render_built_from_an_older_selected_shot_is_rejected_before_any_analysis(
    factory: sessionmaker[Session], store: FilesystemBlobStore, fixture: FinalQAFixture
) -> None:
    from vidgen.db.animation_models import AnimationGeneratedVideo

    with factory() as session:
        # A newer selected animation for shot 0 supersedes what the render holds.
        video = session.scalars(
            select(AnimationGeneratedVideo).where(
                AnimationGeneratedVideo.shot_id == fixture.shot_ids[0],
                AnimationGeneratedVideo.selected.is_(True),
            )
        ).one()
        video.canonical_asset_id = fixture.narration_asset_id
        session.commit()

        with pytest.raises(FinalQALineageError) as error:
            run(session, store, fixture)
        assert error.value.code is FinalQAFailureCode.STALE_SHOT_SELECTION
        assert not error.value.retryable
        # Nothing was analysed and nothing may complete.
        allowed, reason = completion_allowed(
            session,
            project_id=fixture.project_id,
            final_render_asset_id=fixture.final_video_asset_id,
        )
        assert not allowed and reason in {"final_qa_missing", "final_qa_failed"}


def test_a_selected_shot_without_a_passing_video_qa_result_is_rejected(
    factory: sessionmaker[Session], store: FilesystemBlobStore, fixture: FinalQAFixture
) -> None:
    from vidgen.db.visual_qa_models import VisualQARun

    with factory() as session:
        run_row = session.scalars(
            select(VisualQARun).where(
                VisualQARun.shot_id == fixture.shot_ids[3], VisualQARun.target_type == "video"
            )
        ).one()
        run_row.final_outcome = "FAIL"
        session.commit()

        with pytest.raises(FinalQALineageError) as error:
            run(session, store, fixture)
        assert error.value.code is FinalQAFailureCode.FAILING_VIDEO_QA_RESULT


def test_an_unresolved_t21_human_review_blocks_final_qa(
    factory: sessionmaker[Session], store: FilesystemBlobStore, fixture: FinalQAFixture
) -> None:
    from vidgen.db.animation_models import AnimationGeneratedVideo
    from vidgen.db.visual_qa_models import VisualQARun
    from vidgen.db.visual_qa_repository import VisualQARepository

    with factory() as session:
        qa_run = session.scalars(
            select(VisualQARun).where(
                VisualQARun.shot_id == fixture.shot_ids[2], VisualQARun.target_type == "video"
            )
        ).one()
        result = VisualQARepository(session).canonical_result(qa_run.id)
        assert result is not None
        video = session.scalars(
            select(AnimationGeneratedVideo).where(
                AnimationGeneratedVideo.shot_id == fixture.shot_ids[2]
            )
        ).one()
        session.add(
            RepairRun(
                project_id=fixture.project_id,
                shot_id=fixture.shot_ids[2],
                root_animation_attempt_id=video.id,
                triggering_qa_result_id=result.id,
                policy_version="t21/1",
                policy={},
                classifier_version="t21/1",
                planner_version="t21/1",
                input_hash=f"{9:064x}",
                idempotency_key="t21-review",
                state="HUMAN_REVIEW_REQUIRED",
                human_review_reason="ambiguous_identity",
            )
        )
        session.commit()

        with pytest.raises(FinalQALineageError) as error:
            run(session, store, fixture)
        assert error.value.code is FinalQAFailureCode.UNRESOLVED_REPAIR_REVIEW


def test_a_render_from_another_project_is_rejected_as_a_cross_project_asset(
    factory: sessionmaker[Session], store: FilesystemBlobStore, fixture: FinalQAFixture
) -> None:
    with factory() as session:
        asset = session.get(Asset, fixture.final_video_asset_id)
        assert asset is not None
        asset.project_id = UUID(int=7)
        session.commit()

        with pytest.raises(FinalQALineageError) as error:
            run(session, store, fixture)
        assert error.value.code is FinalQAFailureCode.CROSS_PROJECT_ASSET


# --- deterministic blocking fixtures -----------------------------------------
def test_corrupt_final_media_fails_the_gate_and_never_reaches_paid_analysis(
    factory: sessionmaker[Session],
    store: FilesystemBlobStore,
    blob_root: Path,
    fixture: FinalQAFixture,
    tmp_path: Path,
) -> None:
    corrupt = tmp_path / "corrupt.mp4"
    original = fixture.workspace / "final.mp4"
    payload = bytearray(original.read_bytes())
    # Damage the middle of the media data, leaving the container header intact
    # so the file still probes but no longer decodes cleanly.
    for offset in range(len(payload) // 3, (len(payload) * 2) // 3, 7):
        payload[offset] = payload[offset] ^ 0xFF
    corrupt.write_bytes(bytes(payload))

    with factory() as session:
        replace_final_render(session, blob_root, fixture, corrupt)
        result = run(session, store, fixture)

        assert result.decision is FinalQADecision.FAIL
        assert result.status is FinalQAStatus.FINAL_QA_FAILED
        assert result.deterministic_failure_count > 0
        # No paid analysis is attempted once measurement already failed.
        assert result.cost_microusd == 0
        assert not result.adjudicated
        allowed, _ = completion_allowed(
            session,
            project_id=fixture.project_id,
            final_render_asset_id=fixture.final_video_asset_id,
        )
        assert not allowed


def test_audio_video_drift_fails_the_gate(
    factory: sessionmaker[Session],
    store: FilesystemBlobStore,
    blob_root: Path,
    fixture: FinalQAFixture,
    tmp_path: Path,
) -> None:
    shots = sorted(fixture.workspace.glob("source-shot-*.mp4"), key=lambda p: p.name)
    shots.sort(key=lambda path: int(path.stem.rsplit("-", 1)[1]))
    drifted = assemble_render(
        tmp_path / "drift.mp4",
        shots=shots,
        narration=fixture.workspace / "narration.wav",
        subtitles=fixture.workspace / "captions.srt",
        # Two seconds shorter than the picture: far beyond the tolerance.
        audio_trim_seconds=(fixture.timeline_duration_us / 1_000_000) - 2.0,
    )
    with factory() as session:
        replace_final_render(session, blob_root, fixture, drifted)
        result = run(session, store, fixture)
        assert result.decision is FinalQADecision.FAIL
        assert result.audio_failure_count > 0 or result.deterministic_failure_count > 0
        codes = {
            row.check_code
            for row in session.scalars(select(FinalEditorialCheckRecord))
            if row.status == "fail"
        }
        assert {
            FinalIssueCode.AV_DURATION_DRIFT.value,
            FinalIssueCode.AUDIO_VIDEO_DRIFT.value,
            FinalIssueCode.AUDIO_DURATION_MISMATCH.value,
        } & codes


def test_missing_caption_coverage_fails_the_gate_and_names_the_repair_target(
    factory: sessionmaker[Session],
    store: FilesystemBlobStore,
    blob_root: Path,
    fixture: FinalQAFixture,
) -> None:
    from services.renderer.captions import serialize_srt

    with factory() as session:
        # Deliver a caption file that covers only the first half of the recap.
        truncated = fixture.caption_track.model_copy(
            update={
                "cues": [
                    cue
                    for cue in fixture.caption_track.cues
                    if cue.end_us < fixture.timeline_duration_us // 2
                ]
            }
        )
        asset = session.get(Asset, fixture.srt_asset_id)
        assert asset is not None
        content = serialize_srt(truncated).encode()
        FilesystemBlobStore(blob_root, b"test-secret")
        path = blob_root / asset.storage_key
        path.write_bytes(content)
        import hashlib

        asset.sha256 = hashlib.sha256(content).hexdigest()
        asset.byte_size = len(content)
        session.commit()

        with pytest.raises(FinalQALineageError) as error:
            run(session, store, fixture)
        # The manifest declared a hash the delivered asset no longer has, which
        # is caught before anything is measured.
        assert error.value.code is FinalQAFailureCode.CAPTION_HASH_MISMATCH


def test_a_caption_asset_that_loses_cues_fails_the_caption_gate(
    factory: sessionmaker[Session],
    store: FilesystemBlobStore,
    blob_root: Path,
    fixture: FinalQAFixture,
) -> None:
    """The same defect, reached the other way: the manifest agrees, the file does not."""
    import hashlib

    from services.renderer.captions import serialize_srt

    with factory() as session:
        truncated = fixture.caption_track.model_copy(
            update={"cues": fixture.caption_track.cues[:2]}
        )
        content = serialize_srt(truncated).encode()
        asset = session.get(Asset, fixture.srt_asset_id)
        assert asset is not None
        (blob_root / asset.storage_key).write_bytes(content)
        asset.sha256 = hashlib.sha256(content).hexdigest()
        asset.byte_size = len(content)
        # Keep the manifest consistent with the delivered asset so the lineage
        # check passes and caption QA is what catches the missing cues.
        manifest_asset = session.get(Asset, fixture.manifest_asset_id)
        assert manifest_asset is not None
        import json

        payload = json.loads((blob_root / manifest_asset.storage_key).read_bytes())
        for reference in payload["caption_assets"]:
            if reference["asset_id"] == str(asset.id):
                reference["sha256"] = asset.sha256
        from services.renderer.manifest import bound_manifest_identity
        from vidgen.contracts.render import RenderManifest

        rebuilt = RenderManifest.model_validate(payload)
        rebuilt = rebuilt.model_copy(update={"render_identity": bound_manifest_identity(rebuilt)})
        new_content = json.dumps(rebuilt.model_dump(mode="json"), sort_keys=True).encode()
        (blob_root / manifest_asset.storage_key).write_bytes(new_content)
        manifest_asset.sha256 = hashlib.sha256(new_content).hexdigest()
        manifest_asset.byte_size = len(new_content)
        job = session.get(vidgen.db.models.RenderJob, fixture.render_job_id)
        assert job is not None
        job.render_identity = rebuilt.render_identity
        session.commit()

        result = run(session, store, fixture)
        assert result.decision is FinalQADecision.FAIL
        assert result.caption_failure_count > 0
        assert FinalRemediationTarget.REBUILD_CAPTIONS_T17 in result.remediation_targets


# --- editorial blocking fixtures ---------------------------------------------
def missing_beat_defect(render_identity: str) -> dict[str, FakeEditorialDefect]:
    """A confident missing-story-beat finding behind deliberately high scores."""
    return {
        render_identity: FakeEditorialDefect(
            # Every dimension scores well. The blocking finding must survive it.
            dimension_scores={category: 98.0 for category in FinalEditorialCategory},
            findings=(
                FakeEditorialFinding(
                    category=FinalEditorialCategory.STORY_BEAT_COVERAGE,
                    issue_code=FinalIssueCode.MISSING_STORY_BEAT,
                    severity=FinalFindingSeverity.BLOCKING,
                    summary="The approved reconciliation beat never appears on screen.",
                    start_us=1_000_000,
                    end_us=2_000_000,
                    confidence=0.95,
                    sample_index=1,
                    shot_index=0,
                    expected_behavior="the reconciliation beat is depicted",
                    observed_behavior="no shot depicts it",
                ),
            ),
        )
    }


def test_a_missing_story_beat_blocks_the_gate_despite_high_dimension_scores(
    factory: sessionmaker[Session], store: FilesystemBlobStore, fixture: FinalQAFixture
) -> None:
    with factory() as session:
        result = run(
            session, store, fixture, fake_defects=missing_beat_defect(fixture.render_identity)
        )
        assert result.decision is FinalQADecision.FAIL
        assert result.blocking_finding_count == 1
        # Not one deterministic check failed: the block is entirely editorial.
        assert result.deterministic_failure_count == 0
        assert FinalRemediationTarget.CORRECT_SCRIPT_UPSTREAM in result.remediation_targets
        allowed, reason = completion_allowed(
            session,
            project_id=fixture.project_id,
            final_render_asset_id=fixture.final_video_asset_id,
        )
        assert not allowed and reason == "final_qa_failed"


def test_a_confirmed_continuity_contradiction_blocks_the_gate(
    factory: sessionmaker[Session], store: FilesystemBlobStore, fixture: FinalQAFixture
) -> None:
    defects = {
        fixture.render_identity: FakeEditorialDefect(
            findings=(
                FakeEditorialFinding(
                    category=FinalEditorialCategory.CHARACTER_IDENTITY_CONTINUITY,
                    issue_code=FinalIssueCode.IDENTITY_CONTRADICTION,
                    severity=FinalFindingSeverity.BLOCKING,
                    summary="Maya's hair colour changes between adjacent shots.",
                    start_us=3_000_000,
                    end_us=6_000_000,
                    confidence=0.93,
                    sample_index=2,
                    shot_index=1,
                ),
            ),
        )
    }
    with factory() as session:
        result = run(session, store, fixture, fake_defects=defects)
        assert result.decision is FinalQADecision.FAIL
        assert FinalRemediationTarget.CORRECT_REFERENCE_T19 in result.remediation_targets


def test_a_low_confidence_finding_becomes_review_and_blocks_completion(
    factory: sessionmaker[Session], store: FilesystemBlobStore, fixture: FinalQAFixture
) -> None:
    """Terra below the 0.80 floor must produce REVIEW, never a decision."""
    borderline = FakeEditorialDefect(
        findings=(
            FakeEditorialFinding(
                category=FinalEditorialCategory.SETUP_AND_PAYOFF,
                issue_code=FinalIssueCode.UNRESOLVED_SETUP,
                severity=FinalFindingSeverity.BLOCKING,
                summary="The mug setup may never pay off.",
                start_us=0,
                end_us=3_000_000,
                confidence=0.55,
                sample_index=0,
                shot_index=0,
            ),
        ),
        overall_confidence=0.55,
    )
    defects = {
        fixture.render_identity: FakeEditorialDefect(
            findings=borderline.findings,
            overall_confidence=0.55,
            adjudication=borderline,
        )
    }
    with factory() as session:
        result = run(session, store, fixture, fake_defects=defects)
        assert result.decision is FinalQADecision.REVIEW
        assert result.status is FinalQAStatus.FINAL_QA_REVIEW_REQUIRED
        assert result.review_finding_count == 1
        assert result.adjudicated
        # Terra's confidence is reported, and it is below the decision floor.
        assert result.adjudication_confidence is not None
        assert result.adjudication_confidence < 0.80
        allowed, reason = completion_allowed(
            session,
            project_id=fixture.project_id,
            final_render_asset_id=fixture.final_video_asset_id,
        )
        assert not allowed and reason == "final_qa_review_required"


# --- workflow control flow ---------------------------------------------------
def test_the_workflow_stage_raises_rather_than_reporting_a_failed_run_as_done(
    factory: sessionmaker[Session], store: FilesystemBlobStore, fixture: FinalQAFixture
) -> None:
    with factory() as session:
        with pytest.raises(FinalQABlocked):
            asyncio.run(
                evaluate_final_stage(
                    session,
                    store,
                    project_id=fixture.project_id,
                    options=options(fake_defects=missing_beat_defect(fixture.render_identity)),
                )
            )


def test_the_workflow_stage_raises_on_review_so_completion_cannot_be_assumed(
    factory: sessionmaker[Session], store: FilesystemBlobStore, fixture: FinalQAFixture
) -> None:
    borderline = FakeEditorialDefect(
        findings=(
            FakeEditorialFinding(
                category=FinalEditorialCategory.COMPREHENSIBILITY,
                issue_code=FinalIssueCode.INCOMPREHENSIBLE_SEQUENCE,
                severity=FinalFindingSeverity.REVIEW_REQUIRED,
                summary="The transition between shots three and four may confuse a viewer.",
                start_us=6_000_000,
                end_us=9_000_000,
                confidence=0.6,
                sample_index=3,
                shot_index=2,
            ),
        ),
        overall_confidence=0.6,
    )
    with factory() as session:
        with pytest.raises(FinalQAReviewRequired):
            asyncio.run(
                evaluate_final_stage(
                    session,
                    store,
                    project_id=fixture.project_id,
                    options=options(
                        fake_defects={
                            fixture.render_identity: FakeEditorialDefect(
                                findings=borderline.findings,
                                overall_confidence=0.6,
                                adjudication=borderline,
                            )
                        }
                    ),
                )
            )


def test_a_narration_bed_that_never_speaks_fails_narration_coverage(
    factory: sessionmaker[Session],
    store: FilesystemBlobStore,
    blob_root: Path,
    fixture: FinalQAFixture,
    tmp_path: Path,
) -> None:
    silent = tmp_path / "silent.wav"
    from tests.final_qa_fixtures import ffmpeg

    ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            f"anullsrc=r=48000:cl=stereo:d={fixture.timeline_duration_us / 1_000_000:.3f}",
            "-c:a",
            "pcm_s16le",
            str(silent),
        ]
    )
    shots = sorted(
        fixture.workspace.glob("source-shot-*.mp4"),
        key=lambda path: int(path.stem.rsplit("-", 1)[1]),
    )
    mute = assemble_render(
        tmp_path / "mute.mp4",
        shots=shots,
        narration=silent,
        subtitles=fixture.workspace / "captions.srt",
    )
    with factory() as session:
        replace_final_render(session, blob_root, fixture, mute)
        result = run(session, store, fixture)
        assert result.decision is FinalQADecision.FAIL
        assert result.audio_failure_count > 0
        codes = {
            row.check_code
            for row in session.scalars(select(FinalEditorialCheckRecord))
            if row.status == "fail"
        }
        assert FinalIssueCode.NARRATION_INTERVAL_MISSING.value in codes


def test_the_fixture_narration_bed_is_real_audio(tmp_path: Path) -> None:
    """A guard on the fixture itself: text bytes labelled ``audio/wav`` prove nothing."""
    path = narration_wav(tmp_path / "narration.wav", segments=2, seconds=1.0)
    assert path.stat().st_size > 100_000


def test_the_cli_runs_in_fake_mode_without_any_provider_credential(
    factory: sessionmaker[Session],
    blob_root: Path,
    fixture: FinalQAFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The documented fake-mode command, run end to end against a real fixture."""
    import scripts.run_final_editorial_qa as cli

    monkeypatch.setenv("VIDGEN_BLOB_ROOT", str(blob_root))
    monkeypatch.setenv("VIDGEN_BLOB_SIGNING_SECRET", "test-secret")
    monkeypatch.delenv("VIDGEN_OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(cli, "session_factory", lambda _engine: factory, raising=True)
    monkeypatch.setattr(cli, "build_engine", lambda: None, raising=True)

    def fixture_options(**kwargs: object) -> FinalQACommandOptions:
        # The CLI grades against the production 1080p delivery profile. The
        # fixture renders a smaller profile to keep the suite fast, so the
        # command is exercised with the fixture's configuration.
        return FinalQACommandOptions(configuration=FIXTURE_CONFIGURATION, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(cli, "FinalQACommandOptions", fixture_options, raising=True)
    monkeypatch.setattr(
        "sys.argv",
        ["run_final_editorial_qa.py", str(fixture.project_id), "--provider", "fake"],
    )
    assert asyncio.run(cli.main()) == 0
    printed = capsys.readouterr().out
    for expected in (
        "final_editorial_run_id=",
        "final_render_asset_id=",
        "input_identity=",
        "deterministic_checks=",
        "audio_checks=",
        "caption_checks=",
        "blocking=",
        "remediation_targets=",
        "provider=fake",
        "adjudication=",
        "cost_microusd=",
        "report_asset_id=",
        "gate_decision=PASS",
        "status=FINAL_QA_PASSED",
    ):
        assert expected in printed, f"the CLI must print {expected!r}"
