from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from services.analysis.progress import EpisodeAnalysisPhase, calculate_progress
from tests.test_upload_api import create_project
from vidgen.db.episode_analysis_models import EpisodeAnalysisRun, SceneAnalysisCheckpoint
from vidgen.db.models import Asset, SourceVideo
from vidgen.db.workflow_models import EvidencePackageRecord, SceneEvidenceRecord


@pytest.mark.parametrize(
    ("run_status", "completed", "total", "phase", "percentage", "message"),
    [
        ("episode_analysis_pending", 0, 32, "queued", 0.0, "Preparing episode analysis"),
        ("episode_scene_mapping", 0, 32, "scene_analysis", 0.0, "Analyzing scene 1 of 32"),
        ("episode_scene_mapping", 17, 32, "scene_analysis", 42.5, "Analyzing scene 18 of 32"),
        ("episode_scene_mapping", 32, 32, "scene_analysis", 80.0, "Analyzing scene 32 of 32"),
        ("episode_global_reduction", 32, 32, "building_model", 85.0, "Building episode model"),
        (
            "episode_analysis_validating",
            32,
            32,
            "validating",
            95.0,
            "Validating episode analysis",
        ),
        ("episode_analyzed", 32, 32, "completed", 100.0, "Episode analysis complete"),
    ],
)
def test_progress_follows_the_run_status_and_scene_checkpoints(
    run_status: str,
    completed: int,
    total: int,
    phase: str,
    percentage: float,
    message: str,
) -> None:
    progress = calculate_progress(
        run_status=run_status, completed_scene_count=completed, total_scene_count=total
    )
    assert progress.phase == EpisodeAnalysisPhase(phase)
    assert progress.percentage == percentage
    assert progress.message == message
    assert progress.error_code is None


def test_failed_progress_keeps_finished_scenes_and_names_the_error() -> None:
    progress = calculate_progress(
        run_status="episode_analysis_failed",
        completed_scene_count=17,
        total_scene_count=32,
        error_code="SCENE_ANALYSIS_FAILED",
    )
    assert progress.phase is EpisodeAnalysisPhase.FAILED
    assert progress.percentage == 42.5
    assert progress.message == "Episode analysis failed (SCENE_ANALYSIS_FAILED)"
    assert progress.error_code == "SCENE_ANALYSIS_FAILED"


def test_progress_clamps_counts_and_tolerates_missing_totals() -> None:
    clamped = calculate_progress(
        run_status="episode_scene_mapping", completed_scene_count=40, total_scene_count=32
    )
    assert (clamped.completed_scene_count, clamped.total_scene_count) == (32, 32)
    assert clamped.percentage == 80.0
    unknown_total = calculate_progress(
        run_status="episode_scene_mapping", completed_scene_count=0, total_scene_count=0
    )
    assert unknown_total.percentage == 0.0
    assert unknown_total.message == "Analyzing scenes"
    # A status the pipeline never writes must not take the status endpoint down.
    assert (
        calculate_progress(
            run_status="something_new", completed_scene_count=1, total_scene_count=2
        ).phase
        is EpisodeAnalysisPhase.QUEUED
    )


def _seed_run(
    session: Session,
    project_id: UUID,
    *,
    run_status: str,
    scene_count: int,
    checkpoint_statuses: list[str],
    run_updated_at: datetime,
) -> EpisodeAnalysisRun:
    """Persist the rows the pipeline would have written part-way through a run."""
    asset = Asset(
        project_id=project_id,
        kind="source_video",
        sha256="0" * 64,
        byte_size=1,
        media_type="video/mp4",
        storage_key=f"source/{uuid4()}",
    )
    session.add(asset)
    session.flush()
    source = SourceVideo(
        project_id=project_id, asset_id=asset.id, filename="episode.mp4", duration_seconds=90
    )
    session.add(source)
    session.flush()
    evidence = EvidencePackageRecord(
        project_id=project_id,
        version=1,
        selected=True,
        input_hash="a" * 64,
        schema_version="1.0",
        source_video_id=source.id,
        source_video_asset_id=asset.id,
        transcript_id=uuid4(),
        transcript_asset_id=asset.id,
        transcript_origin="subtitle",
        provenance={},
    )
    session.add(evidence)
    session.flush()
    scenes = [
        SceneEvidenceRecord(
            evidence_package_id=evidence.id,
            scene_sequence=index,
            source_start_seconds=float(index),
            source_end_seconds=float(index + 1),
            frame_asset_ids=[],
            evidence={},
        )
        for index in range(scene_count)
    ]
    session.add_all(scenes)
    run = EpisodeAnalysisRun(
        project_id=project_id,
        source_video_id=source.id,
        evidence_package_id=evidence.id,
        idempotency_key=f"analysis-{uuid4()}",
        input_hash="a" * 64,
        contract_version="1.0",
        prompt_version="episode-analysis-v1",
        provider_configuration_version="episode-provider-v1",
        provider="fake",
        model="fake",
        status=run_status,
        attempt_count=1,
        created_at=run_updated_at,
        updated_at=run_updated_at,
    )
    session.add(run)
    session.flush()
    for index, checkpoint_status in enumerate(checkpoint_statuses):
        session.add(
            SceneAnalysisCheckpoint(
                analysis_run_id=run.id,
                source_scene_id=scenes[index].id,
                sequence=index + 1,
                input_hash="b" * 64,
                idempotency_key=f"{run.idempotency_key}:scene:{index}",
                status=checkpoint_status,
                attempt_count=1,
                created_at=run_updated_at,
                updated_at=run_updated_at + timedelta(seconds=index),
            )
        )
    session.commit()
    return run


def test_status_endpoint_reports_scene_progress_from_checkpoints(
    api_client: tuple[TestClient, sessionmaker[Session]],
) -> None:
    client, factory = api_client
    project = create_project(client)
    before = client.get(f"/api/v1/projects/{project['id']}/status").json()
    assert before["episode_analysis"] is None

    started = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)
    # 17 scenes are done; one is being retried after an invalid draft and the
    # rest have not been submitted, so the owner is waiting on scene 18.
    statuses = ["succeeded"] * 17 + ["invalid"] + ["pending"] * 14
    with factory() as session:
        _seed_run(
            session,
            UUID(project["id"]),
            run_status="episode_scene_mapping",
            scene_count=32,
            checkpoint_statuses=statuses,
            run_updated_at=started,
        )

    progress = client.get(f"/api/v1/projects/{project['id']}/status").json()["episode_analysis"]
    assert progress == {
        "phase": "scene_analysis",
        "completed_scene_count": 17,
        "total_scene_count": 32,
        "percentage": 42.5,
        "message": "Analyzing scene 18 of 32",
        "error_code": None,
        # The newest checkpoint, not the run row, is the last thing that moved.
        "updated_at": (started + timedelta(seconds=31)).isoformat().replace("+00:00", "Z"),
    }


def test_status_endpoint_counts_evidence_scenes_before_checkpoints_exist(
    api_client: tuple[TestClient, sessionmaker[Session]],
) -> None:
    client, factory = api_client
    project = create_project(client)
    queued_at = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)
    with factory() as session:
        _seed_run(
            session,
            UUID(project["id"]),
            run_status="episode_analysis_pending",
            scene_count=6,
            checkpoint_statuses=[],
            run_updated_at=queued_at,
        )

    progress = client.get(f"/api/v1/projects/{project['id']}/status").json()["episode_analysis"]
    assert progress["phase"] == "queued"
    assert progress["message"] == "Preparing episode analysis"
    assert (progress["completed_scene_count"], progress["total_scene_count"]) == (0, 6)
    assert progress["percentage"] == 0.0
    assert progress["updated_at"] == "2026-08-01T09:00:00Z"
