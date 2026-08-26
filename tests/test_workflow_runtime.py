from collections.abc import Awaitable, Callable
from pathlib import Path
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
from workers.temporal_worker.production_handlers import build_production_handlers


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
    activities.configure_activity_handlers(
        {
            "upload": lambda request: StageActivityResult(
                stage=request.stage,
                entity_id=request.source_video_id,
            )
        }
    )
    request = StageActivityInput(
        project_id=uuid4(),
        source_video_id=uuid4(),
        stage="upload",
        idempotency_key="workflow:upload",
    )
    assert activities.run_upload_activity(request).entity_id == request.source_video_id
    assert heartbeats == [{"stage": "upload"}]


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
    }


@pytest.mark.asyncio
async def test_cancellation_during_final_activity_is_not_reported_as_success() -> None:
    evidence_started = __import__("asyncio").Event()
    release_evidence = __import__("asyncio").Event()

    async def finish(request: StageActivityInput) -> StageActivityResult:
        if request.stage == "evidence":
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
