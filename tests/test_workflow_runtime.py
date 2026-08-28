from collections.abc import Awaitable, Callable
from pathlib import Path
from time import sleep
from uuid import UUID, uuid4

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
    build_shot_production_handlers,
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
        "script_generation",
        "narration",
        "storyboard",
        "image_generation",
    }


def test_production_worker_configures_every_t16_activity(tmp_path: Path) -> None:
    handlers = build_shot_production_handlers(
        APISettings(
            database_url=f"sqlite:///{tmp_path / 'shot-worker.db'}",
            blob_root=tmp_path / "blobs",
            temporal_allow_fake_providers=True,
        )
    )
    assert set(handlers) == {
        "resolve_shot_fanout",
        "resolve_shot_input",
        "run_shot_keyframe",
        "run_shot_keyframe_qa",
        "run_shot_animation",
        "run_shot_video_qa",
        # T21 repair for a shot T20 blocked.
        "run_shot_repair",
        "persist_shot_checkpoint",
        "persist_shot_fanout_checkpoint",
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
            "run_script_generation_activity",
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


def test_a_keyless_worker_refuses_to_run_visual_qa_with_the_fake_agent(
    tmp_path: Path,
) -> None:
    """The deterministic fake always passes, so it must never be a silent default.

    Without a configured provider and without ``temporal_allow_fake_providers``,
    the T20 gate must fail loudly rather than persist fabricated PASS rows as
    canonical provenance.
    """
    from unittest.mock import patch

    strict = APISettings(
        database_url=f"sqlite:///{tmp_path / 'keyless.db'}",
        blob_root=tmp_path / "blobs",
        temporal_allow_fake_providers=False,
        openai_api_key=None,
    )
    from workers.temporal_worker import production_handlers as module

    with patch.object(module, "_authoritative_shot", return_value=(None, None)):
        with pytest.raises(ValueError, match="visual-QA provider is not configured"):
            module._run_shot_video_qa(
                None,  # type: ignore[arg-type]
                None,  # type: ignore[arg-type]
                strict,
                None,  # type: ignore[arg-type]
                None,  # type: ignore[arg-type]
                _shot_workflow_input().model_dump(mode="json"),
            )


def _shot_workflow_input() -> object:
    """A minimal valid T16 input for the keyless-worker guard test."""
    from packages.workflows.shot_policy import identity_hash
    from vidgen.contracts.shot_workflow import ShotWorkflowIdentity, ShotWorkflowInput

    material = {
        "project_id": str(UUID(int=1)),
        "storyboard_run_id": str(UUID(int=2)),
        "storyboard_input_hash": "a" * 64,
        "storyboard_shot_id": str(UUID(int=3)),
        "canonical_shot_hash": "b" * 64,
        "shot_sequence": 0,
        "timing_manifest_hash": "c" * 64,
        "t14_configuration_identity": "fake:image:1",
        "t15_capability_profile_identity": "fake:video:1",
        "t14_pipeline_version": "image-generation/1.0.0",
        "t15_pipeline_version": "animation/1.0.0",
        "t16_workflow_version": "t16/1",
        "attempt_policy_version": "shot-attempt/1",
    }
    identity = ShotWorkflowIdentity.model_validate(
        {**material, "identity_hash": identity_hash(material)}
    )
    return ShotWorkflowInput(
        project_id=identity.project_id,
        storyboard_run_id=identity.storyboard_run_id,
        storyboard_shot_id=identity.storyboard_shot_id,
        shot_input_hash=identity.identity_hash,
        workflow_identity=identity,
        idempotency_key="keyless-guard",
    )
