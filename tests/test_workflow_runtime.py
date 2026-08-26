from collections.abc import Awaitable, Callable
from pathlib import Path
from time import sleep
from uuid import uuid4

import pytest
from pydantic import ValidationError
from temporalio import activity
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from apps.api.settings import APISettings
from packages.workflows import activities
from packages.workflows.project import ProjectWorkflow
from vidgen.contracts.workflow import ProjectWorkflowInput, StageActivityInput, StageActivityResult
from vidgen.db.transcription_models import SpeakerTurnRecord, TranscriptSegmentRecord
from workers.temporal_worker.production_handlers import (
    _segments_with_speakers,
    build_production_handlers,
)


def test_workflow_key_reserves_generated_stage_suffix_space() -> None:
    valid = ProjectWorkflowInput(
        project_id=uuid4(),
        source_video_id=uuid4(),
        idempotency_key="x" * 220,
    )
    assert len(f"{valid.idempotency_key}:transcript_acquisition") <= 255
    with pytest.raises(ValidationError):
        ProjectWorkflowInput(
            project_id=uuid4(),
            source_video_id=uuid4(),
            idempotency_key="x" * 221,
        )


def test_synchronous_activity_heartbeats_while_handler_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    heartbeats: list[object] = []
    monkeypatch.setattr(activity, "heartbeat", heartbeats.append)
    monkeypatch.setattr(activities, "HEARTBEAT_INTERVAL_SECONDS", 0.005)
    activities.configure_activity_handlers({"upload": _slow_result})
    request = StageActivityInput(
        project_id=uuid4(),
        source_video_id=uuid4(),
        stage="upload",
        idempotency_key="workflow:upload",
    )
    assert activities.run_upload_activity(request).entity_id == request.source_video_id
    assert len(heartbeats) >= 2
    assert all(item == {"stage": "upload"} for item in heartbeats)


def _slow_result(request: StageActivityInput) -> StageActivityResult:
    sleep(0.02)
    return StageActivityResult(stage=request.stage, entity_id=request.source_video_id)


def test_production_worker_configures_every_workflow_stage(tmp_path: Path) -> None:
    handlers = build_production_handlers(
        APISettings(
            database_url=f"sqlite:///{tmp_path / 'worker.db'}",
            blob_root=tmp_path / "blobs",
        )
    )
    assert set(handlers) == {
        "upload",
        "media_processing",
        "transcript_acquisition",
        "evidence",
        "episode_analysis",
    }


def test_audio_segments_are_split_across_overlapping_speaker_turns() -> None:
    chunk_id = uuid4()
    segment = TranscriptSegmentRecord(
        transcript_id=uuid4(),
        sequence=0,
        start_seconds=1,
        end_seconds=4,
        text="shared dialogue",
        speaker_label=None,
        confidence=None,
        source_chunk_ids=[str(chunk_id)],
        words=[],
        provenance={},
    )
    turns = [
        SpeakerTurnRecord(
            transcript_id=segment.transcript_id,
            sequence=sequence,
            speaker_label=f"speaker_{sequence:03d}",
            start_seconds=start,
            end_seconds=end,
            confidence=0.9,
            source_chunk_ids=[str(chunk_id)],
            provider_metadata={},
            alternate_mappings=[],
            warnings=[],
        )
        for sequence, start, end in ((0, 1.0, 3.0), (1, 2.0, 4.0))
    ]
    evidence_segments = _segments_with_speakers([segment], turns)
    assert [item.speaker_label for item in evidence_segments] == ["speaker_000", "speaker_001"]
    assert [(item.start_seconds, item.end_seconds) for item in evidence_segments] == [
        (1, 3),
        (2, 4),
    ]


@pytest.mark.asyncio
async def test_cancellation_during_final_activity_is_not_reported_as_success() -> None:
    evidence_started = __import__("asyncio").Event()
    release_evidence = __import__("asyncio").Event()

    async def finish(request: StageActivityInput) -> StageActivityResult:
        if request.stage == "episode_analysis":
            evidence_started.set()
            await release_evidence.wait()
        return StageActivityResult(stage=request.stage)

    def named_activity(
        name: str,
    ) -> Callable[[StageActivityInput], Awaitable[StageActivityResult]]:
        async def execute(request: StageActivityInput) -> StageActivityResult:
            return await finish(request)

        return activity.defn(name=name)(execute)

    fake_activities = [
        named_activity(name)
        for name in (
            "run_upload_activity",
            "run_media_processing_activity",
            "run_transcript_acquisition_activity",
            "run_evidence_activity",
            "run_episode_analysis_activity",
        )
    ]

    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as environment:
        async with Worker(
            environment.client,
            task_queue="workflow-cancellation-test",
            workflows=[ProjectWorkflow],
            activities=fake_activities,
        ):
            handle = await environment.client.start_workflow(
                ProjectWorkflow.run,
                ProjectWorkflowInput(
                    project_id=uuid4(),
                    source_video_id=uuid4(),
                    idempotency_key="cancel-final-stage",
                ),
                id=f"test-{uuid4()}",
                task_queue="workflow-cancellation-test",
            )
            await evidence_started.wait()
            await handle.signal(ProjectWorkflow.cancel_project)
            release_evidence.set()
            result = await handle.result()
            assert result.cancelled
            assert result.status == "cancelled"
