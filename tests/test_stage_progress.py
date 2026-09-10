from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String, Table, Uuid, insert, inspect
from sqlalchemy.orm import Session, sessionmaker

from services.progress import specs
from services.progress.engine import ProgressState, calculate_progress
from tests.test_upload_api import create_project
from vidgen.db.animation_models import AnimationGeneratedVideo
from vidgen.db.continuity_models import (
    character_identity_versions,
    character_reference_candidates,
    character_reference_sets,
)
from vidgen.db.final_editorial_models import FinalEditorialRun
from vidgen.db.image_generation_models import GeneratedKeyframeImage
from vidgen.db.models import Asset, Project, RenderJob, Scene
from vidgen.db.narration_models import NarrationRun, NarrationSegment
from vidgen.db.script_models import Script, ScriptGenerationRun, ScriptReview, ScriptSegment
from vidgen.db.storyboard_models import (
    StoryboardRun,
    StoryboardSegmentCheckpoint,
    StoryboardShotRecord,
)
from vidgen.db.transcription_models import TranscriptionChunk, TranscriptionRun
from vidgen.db.visual_qa_models import VisualQARun
from vidgen.db.workflow_models import EvidencePackageRecord, SceneEvidenceRecord

T0 = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)


def at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


def _placeholder(column: Any) -> Any:
    """A value that satisfies the column's type and the repo's check constraints.

    Strings are unique so idempotency keys and identities never collide, hashes
    are 64 characters long, and start/end pairs are ordered.
    """
    kind = column.type
    name = column.name
    if isinstance(kind, String):
        return uuid4().hex * 2 if name.endswith(("hash", "sha256", "identity")) else uuid4().hex
    if isinstance(kind, Uuid):
        return uuid4()
    if isinstance(kind, Boolean):
        return False
    if isinstance(kind, Integer | Float):
        value = 2 if "end" in name else 0 if "start" in name else 1
        return float(value) if isinstance(kind, Float) else value
    if isinstance(kind, JSON):
        return {}
    if isinstance(kind, DateTime):
        return T0
    raise TypeError(f"no placeholder for {name}: {kind}")


def seed(session: Session, model: Any, **values: Any) -> Any:
    """Insert one row, filling every required column the test does not care about.

    Progress reads a handful of columns per table; the rest exist only to
    satisfy NOT NULL, so they get neutral placeholders rather than a full
    fixture graph. SQLite does not enforce foreign keys here.
    """
    if isinstance(model, Table):
        row = dict(values)
        for column in model.columns:
            if column.name not in row and not column.nullable and column.default is None:
                row[column.name] = uuid4() if column.primary_key else _placeholder(column)
        session.execute(insert(model).values(**row))
        return row
    for column in inspect(model).columns:
        key = column.key
        if key in values or column.nullable or column.primary_key:
            continue
        if column.default is not None or column.server_default is not None:
            continue
        values[key] = _placeholder(column)
    row = model(**values)
    session.add(row)
    session.flush()
    return row


def _set_status(session: Session, project_id: UUID, status: str, when: datetime) -> Project:
    project = session.get(Project, project_id)
    assert project is not None
    project.status = status
    project.updated_at = when
    session.flush()
    return project


def _progress(client: TestClient, project_id: str) -> dict[str, Any] | None:
    response = client.get(f"/api/v1/projects/{project_id}/status")
    assert response.status_code == 200
    progress: dict[str, Any] | None = response.json()["stage_progress"]
    return progress


# --------------------------------------------------------------------------
# The engine
# --------------------------------------------------------------------------


def test_counted_phase_fills_its_share_and_names_the_next_item() -> None:
    progress = calculate_progress(
        specs.NARRATION, status="narration_generating", completed_count=5, total_count=12
    )
    assert progress.state is ProgressState.RUNNING
    assert progress.message == "Generating narration 6 of 12"
    assert progress.percentage == 38.3
    assert (progress.completed_count, progress.total_count) == (5, 12)
    assert progress.count_label == "segments narrated"


def test_share_overrides_the_counted_fraction() -> None:
    progress = calculate_progress(
        specs.RENDERING, status="render_rendering", completed_count=0, total_count=0, share=0.4
    )
    assert progress.percentage == 40.0
    assert progress.message == "Rendering the final cut"


def test_failed_stage_keeps_the_share_it_earned_and_names_the_error() -> None:
    progress = calculate_progress(
        specs.STORYBOARD,
        status="storyboard_failed",
        completed_count=3,
        total_count=6,
        error_code="DirectorError",
    )
    assert progress.state is ProgressState.FAILED
    assert progress.percentage == 42.5
    assert progress.message == "Storyboard failed (DirectorError)"
    assert progress.error_code == "DirectorError"


def test_human_gates_read_as_waiting_and_unknown_statuses_as_queued() -> None:
    waiting = calculate_progress(
        specs.SCRIPT_GENERATION, status="script_review_required", completed_count=2, total_count=3
    )
    assert waiting.state is ProgressState.WAITING
    assert waiting.message == "Script needs your review"
    unknown = calculate_progress(
        specs.NARRATION, status="something_new", completed_count=1, total_count=2
    )
    assert unknown.state is ProgressState.QUEUED
    assert unknown.phase == "queued"


def test_a_counted_message_drops_its_numbers_until_there_is_a_total() -> None:
    progress = calculate_progress(
        specs.TRANSCRIPTION, status="transcribing", completed_count=0, total_count=0
    )
    assert progress.message == "Transcribing chunk"
    assert progress.percentage == 10.0


# --------------------------------------------------------------------------
# Each stage, read through the status endpoint
# --------------------------------------------------------------------------


def test_no_stage_has_started(api_client: tuple[TestClient, sessionmaker[Session]]) -> None:
    client, _ = api_client
    project = create_project(client)
    assert _progress(client, project["id"]) is None


def test_media_processing_counts_frames_against_detected_scenes(
    api_client: tuple[TestClient, sessionmaker[Session]],
) -> None:
    client, factory = api_client
    project_id = UUID(create_project(client)["id"])
    with factory() as session:
        _set_status(session, project_id, "extracting_frames", at(1))
        for index in range(10):
            seed(
                session,
                Scene,
                project_id=project_id,
                sequence=index,
                source_start_seconds=float(index),
                source_end_seconds=float(index + 1),
                summary="scene",
            )
        for _ in range(3):
            seed(session, Asset, project_id=project_id, kind="frame", storage_key=str(uuid4()))
        session.commit()
    progress = _progress(client, str(project_id))
    assert progress is not None
    assert progress["stage"] == "media_processing"
    assert progress["message"] == "Extracting frames for scene 4 of 10"
    assert progress["percentage"] == 70.5
    assert progress["count_label"] == "scenes with frames"
    assert progress["updated_at"] == "2026-08-01T09:01:00Z"


def test_transcription_counts_completed_chunks(
    api_client: tuple[TestClient, sessionmaker[Session]],
) -> None:
    client, factory = api_client
    project_id = UUID(create_project(client)["id"])
    with factory() as session:
        _set_status(session, project_id, "transcribing", at(2))
        run = seed(
            session,
            TranscriptionRun,
            project_id=project_id,
            status="transcribing",
            created_at=at(1),
            updated_at=at(2),
        )
        for index in range(8):
            seed(
                session,
                TranscriptionChunk,
                run_id=run.id,
                sequence=index,
                status="complete" if index < 4 else "pending",
                updated_at=at(3) if index == 3 else at(1),
            )
        session.commit()
    progress = _progress(client, str(project_id))
    assert progress is not None
    assert progress["stage"] == "transcript_acquisition"
    assert progress["state"] == "running"
    assert progress["message"] == "Transcribing chunk 5 of 8"
    assert progress["percentage"] == 45.0
    assert progress["updated_at"] == "2026-08-01T09:03:00Z"


def test_evidence_is_pending_then_ready(
    api_client: tuple[TestClient, sessionmaker[Session]],
) -> None:
    client, factory = api_client
    project_id = UUID(create_project(client)["id"])
    with factory() as session:
        _set_status(session, project_id, "transcribed", at(1))
        for index in range(10):
            seed(
                session,
                Scene,
                project_id=project_id,
                sequence=index,
                source_start_seconds=float(index),
                source_end_seconds=float(index + 1),
                summary="scene",
            )
        session.commit()
    pending = _progress(client, str(project_id))
    assert pending is not None
    assert (pending["stage"], pending["state"]) == ("evidence", "queued")
    assert pending["message"] == "Waiting to assemble evidence for 10 scenes"

    with factory() as session:
        package = seed(
            session,
            EvidencePackageRecord,
            project_id=project_id,
            selected=True,
            provenance={},
            created_at=at(2),
            updated_at=at(2),
        )
        for index in range(9):
            seed(
                session,
                SceneEvidenceRecord,
                evidence_package_id=package.id,
                scene_sequence=index,
                source_start_seconds=float(index),
                source_end_seconds=float(index + 1),
                frame_asset_ids=[],
                evidence={},
            )
        session.commit()
    ready = _progress(client, str(project_id))
    assert ready is not None
    assert (ready["stage"], ready["state"]) == ("evidence", "completed")
    assert ready["message"] == "Evidence package ready for 10 scenes"
    assert (ready["completed_count"], ready["total_count"]) == (9, 10)


def test_script_generation_counts_editorial_passes(
    api_client: tuple[TestClient, sessionmaker[Session]],
) -> None:
    client, factory = api_client
    project_id = UUID(create_project(client)["id"])
    with factory() as session:
        _set_status(session, project_id, "comedy_editing", at(3))
        run = seed(
            session,
            ScriptGenerationRun,
            project_id=project_id,
            status="comedy_editing",
            created_at=at(1),
            updated_at=at(3),
        )
        script = seed(
            session,
            Script,
            project_id=project_id,
            generation_run_id=run.id,
            episode_analysis_id=uuid4(),
            version=1,
            status="draft",
        )
        seed(session, ScriptReview, script_id=script.id, review_sequence=1, attempt_number=1)
        session.commit()
    progress = _progress(client, str(project_id))
    assert progress is not None
    assert progress["stage"] == "script_generation"
    assert progress["message"] == "Editing pass 2 of 3"
    assert progress["percentage"] == 78.3


def test_narration_counts_segments_against_the_approved_script(
    api_client: tuple[TestClient, sessionmaker[Session]],
) -> None:
    client, factory = api_client
    project_id = UUID(create_project(client)["id"])
    with factory() as session:
        # The project status carries the finer phase the run row never sees.
        _set_status(session, project_id, "narration_aligning", at(4))
        script = seed(
            session, Script, project_id=project_id, version=1, status="approved", selected=True
        )
        for index in range(12):
            seed(session, ScriptSegment, script_id=script.id, sequence=index, text="line")
        run = seed(
            session,
            NarrationRun,
            project_id=project_id,
            script_id=script.id,
            status="narration_generating",
            created_at=at(1),
            updated_at=at(2),
        )
        # Segments are created lazily, so only six rows exist so far.
        for index in range(6):
            seed(
                session,
                NarrationSegment,
                narration_run_id=run.id,
                sequence=index,
                status="complete" if index < 5 else "pending",
                updated_at=at(5) if index == 4 else at(2),
            )
        session.commit()
    progress = _progress(client, str(project_id))
    assert progress is not None
    assert progress["stage"] == "narration"
    assert progress["message"] == "Aligning narration 6 of 12"
    assert (progress["completed_count"], progress["total_count"]) == (5, 12)
    assert progress["percentage"] == 38.3
    assert progress["updated_at"] == "2026-08-01T09:05:00Z"


def test_storyboard_counts_directed_segments(
    api_client: tuple[TestClient, sessionmaker[Session]],
) -> None:
    client, factory = api_client
    project_id = UUID(create_project(client)["id"])
    with factory() as session:
        _set_status(session, project_id, "storyboard_directing", at(2))
        script = seed(session, Script, project_id=project_id, version=1, status="approved")
        for index in range(6):
            seed(session, ScriptSegment, script_id=script.id, sequence=index, text="line")
        run = seed(
            session,
            StoryboardRun,
            project_id=project_id,
            script_id=script.id,
            status="storyboard_directing",
            created_at=at(1),
            updated_at=at(2),
        )
        for index in range(3):
            seed(
                session,
                StoryboardSegmentCheckpoint,
                storyboard_run_id=run.id,
                sequence=index + 1,
                status="complete" if index < 2 else "directing",
            )
        session.commit()
    progress = _progress(client, str(project_id))
    assert progress is not None
    assert progress["stage"] == "storyboard"
    assert progress["message"] == "Directing segment 3 of 6"
    assert progress["percentage"] == 28.3


def test_reference_sheets_are_generated_then_wait_for_approval(
    api_client: tuple[TestClient, sessionmaker[Session]],
) -> None:
    client, factory = api_client
    project_id = UUID(create_project(client)["id"])
    with factory() as session:
        _set_status(session, project_id, "storyboard_complete", at(1))
        versions = [
            seed(
                session,
                character_identity_versions,
                project_id=project_id,
                character_id=uuid4(),
                status="draft",
                created_at=at(2),
                updated_at=at(2),
            )
            for _ in range(3)
        ]
        for version in versions:
            seed(
                session,
                character_reference_candidates,
                identity_version_id=version["id"],
                selected=True,
                created_at=at(2),
            )
        for version in versions[:2]:
            seed(
                session,
                character_reference_sets,
                project_id=project_id,
                identity_version_id=version["id"],
                status="draft",
                ordered_asset_ids=[],
                validation_report={},
                created_at=at(3),
                updated_at=at(3),
            )
        session.commit()
    generating = _progress(client, str(project_id))
    assert generating is not None
    assert (generating["stage"], generating["state"]) == ("references", "running")
    assert generating["message"] == "Generating reference sheet 3 of 3"
    assert generating["percentage"] == 46.7

    with factory() as session:
        seed(
            session,
            character_reference_sets,
            project_id=project_id,
            identity_version_id=versions[2]["id"],
            status="approved",
            ordered_asset_ids=[],
            validation_report={},
            created_at=at(4),
            updated_at=at(4),
        )
        session.commit()
    waiting = _progress(client, str(project_id))
    assert waiting is not None
    assert waiting["state"] == "waiting"
    assert waiting["message"] == ("1 of 3 reference sheets approved; the rest need your approval")
    assert waiting["count_label"] == "reference sheets approved"


def _storyboard_with_shots(session: Session, project_id: UUID, shots: int) -> list[UUID]:
    run = seed(
        session,
        StoryboardRun,
        project_id=project_id,
        status="storyboard_complete",
        selected=True,
        shot_count=shots,
        created_at=at(1),
        updated_at=at(1),
    )
    return [
        seed(
            session,
            StoryboardShotRecord,
            storyboard_run_id=run.id,
            segment_checkpoint_id=uuid4(),
            global_sequence=index,
            segment_sequence=index,
            start_us=0,
            end_us=2,
            usable_duration_us=2,
            requested_generation_duration_us=2,
            references={},
            contract={},
            provenance={},
        ).id
        for index in range(shots)
    ]


def test_shot_generation_reports_animation_then_quality_review(
    api_client: tuple[TestClient, sessionmaker[Session]],
) -> None:
    client, factory = api_client
    project_id = UUID(create_project(client)["id"])
    with factory() as session:
        _set_status(session, project_id, "animation_polling", at(5))
        shot_ids = _storyboard_with_shots(session, project_id, 20)
        for shot_id in shot_ids:
            seed(
                session,
                GeneratedKeyframeImage,
                project_id=project_id,
                shot_id=shot_id,
                keyframe_role="FIRST_FRAME",
                selected=True,
                validation_report={},
            )
        for shot_id in shot_ids[:8]:
            seed(
                session,
                AnimationGeneratedVideo,
                project_id=project_id,
                shot_id=shot_id,
                selected=True,
                validation_report={},
                trim_manifest={},
            )
        session.commit()
    animating = _progress(client, str(project_id))
    assert animating is not None
    assert (animating["stage"], animating["label"]) == ("shot_generation", "Shot generation")
    assert animating["message"] == "Animating shot 9 of 20"
    assert (animating["completed_count"], animating["total_count"]) == (8, 20)
    assert animating["count_label"] == "shots animated"
    # Keyframes, animation and review each own a third of the bar.
    assert animating["percentage"] == 46.7

    with factory() as session:
        _set_status(session, project_id, "shot_generation_running", at(6))
        for shot_id in shot_ids[8:]:
            seed(
                session,
                AnimationGeneratedVideo,
                project_id=project_id,
                shot_id=shot_id,
                selected=True,
                validation_report={},
                trim_manifest={},
            )
        for shot_id in shot_ids[:5]:
            seed(
                session,
                VisualQARun,
                project_id=project_id,
                shot_id=shot_id,
                target_type="video",
                status="visual_qa_complete",
                importance="normal",
                deterministic_report={},
                repair_codes=[],
                warning_codes=[],
                updated_at=at(7),
            )
        session.commit()
    reviewing = _progress(client, str(project_id))
    assert reviewing is not None
    assert (reviewing["stage"], reviewing["label"]) == ("shot_generation", "Quality review")
    assert reviewing["message"] == "Reviewing shot 6 of 20"
    assert reviewing["count_label"] == "shots reviewed"
    assert reviewing["percentage"] == 75.0
    assert reviewing["updated_at"] == "2026-08-01T09:07:00Z"


def test_rendering_uses_the_job_checkpoint_and_wins_over_finished_shots(
    api_client: tuple[TestClient, sessionmaker[Session]],
) -> None:
    client, factory = api_client
    project_id = UUID(create_project(client)["id"])
    with factory() as session:
        # Rendering never writes the project status, which still says the
        # shots finished; the newer render job is what the owner waits on.
        _set_status(session, project_id, "shot_generation_complete", at(3))
        _storyboard_with_shots(session, project_id, 2)
        seed(
            session,
            RenderJob,
            project_id=project_id,
            status="render_rendering",
            progress_percent=40,
            checkpoint="ffmpeg",
            created_at=at(4),
            updated_at=at(4),
            heartbeat_at=at(5),
        )
        session.commit()
    progress = _progress(client, str(project_id))
    assert progress is not None
    assert (progress["stage"], progress["state"]) == ("rendering", "running")
    assert progress["message"] == "Rendering the final cut"
    assert progress["percentage"] == 40.0
    assert progress["updated_at"] == "2026-08-01T09:05:00Z"


def test_final_quality_check_counts_completed_phases_and_review_gate(
    api_client: tuple[TestClient, sessionmaker[Session]],
) -> None:
    client, factory = api_client
    project_id = UUID(create_project(client)["id"])
    with factory() as session:
        run = seed(
            session,
            FinalEditorialRun,
            project_id=project_id,
            status="FINAL_QA_CHECKING_CAPTIONS",
            current_phase="CAPTION_QA",
            completed_phases=["INPUT_VALIDATION", "DETERMINISTIC_MEDIA_QA"],
            remediation_targets=[],
            created_at=at(1),
            updated_at=at(2),
        )
        session.commit()
        run_id = run.id
    checking = _progress(client, str(project_id))
    assert checking is not None
    assert (checking["stage"], checking["label"]) == ("final_qa", "Final quality check")
    assert checking["message"] == "Checking captions (2 of 6 checks done)"
    assert checking["percentage"] == 31.7

    with factory() as session:
        run = session.get(FinalEditorialRun, run_id)
        assert run is not None
        run.status = "FINAL_QA_REVIEW_REQUIRED"
        run.completed_phases = [*run.completed_phases, "CAPTION_QA", "EDITORIAL_ANALYSIS"]
        session.commit()
    waiting = _progress(client, str(project_id))
    assert waiting is not None
    assert (waiting["state"], waiting["percentage"]) == ("waiting", 100.0)
    assert waiting["message"] == "Final quality check needs your review"
