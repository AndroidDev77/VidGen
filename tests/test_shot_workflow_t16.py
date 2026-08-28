from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from uuid import UUID

import pytest
from pydantic import ValidationError
from temporalio import activity
from temporalio.client import WorkflowHandle
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ApplicationError
from temporalio.service import RPCError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from packages.workflows.shot import ProjectShotFanoutWorkflow, ShotWorkflow
from packages.workflows.shot_policy import (
    identity_hash,
    shot_activity_idempotency_key,
    temporal_shot_workflow_id,
)
from vidgen.contracts.shot_workflow import (
    ProjectShotFanoutInput,
    ProjectShotFanoutResult,
    ResolveShotFanoutResult,
    ShotWorkflowCommand,
    ShotWorkflowIdentity,
    ShotWorkflowInput,
    ShotWorkflowProgress,
    ShotWorkflowResult,
    ShotWorkflowStatus,
)

PROJECT = UUID("00000000-0000-0000-0000-000000000001")
STORYBOARD = UUID("00000000-0000-0000-0000-000000000002")
SHOT = UUID("00000000-0000-0000-0000-000000000003")
HASH = "a" * 64


def make_identity(canonical_hash: str = HASH) -> ShotWorkflowIdentity:
    fields: dict[str, str | int] = {
        "project_id": str(PROJECT),
        "storyboard_run_id": str(STORYBOARD),
        "storyboard_input_hash": HASH,
        "storyboard_shot_id": str(SHOT),
        "canonical_shot_hash": canonical_hash,
        "shot_sequence": 0,
        "timing_manifest_hash": HASH,
        "t14_configuration_identity": "image-provider/1",
        "t15_capability_profile_identity": "runway/2024-11-06",
        "t14_pipeline_version": "t14/1",
        "t15_pipeline_version": "t15/1",
        "t16_workflow_version": "t16/1",
        "attempt_policy_version": "shot-attempt/1",
    }
    return ShotWorkflowIdentity(**fields, identity_hash=identity_hash(fields))


def test_identity_is_stable_and_material_change_creates_new_child() -> None:
    first = make_identity()
    assert first == make_identity()
    changed = make_identity("b" * 64)
    assert first.identity_hash != changed.identity_hash
    assert temporal_shot_workflow_id(first) != temporal_shot_workflow_id(changed)
    assert temporal_shot_workflow_id(first) == temporal_shot_workflow_id(make_identity())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("storyboard_input_hash", "b" * 64),
        ("canonical_shot_hash", "b" * 64),
        ("shot_sequence", 2),
        ("timing_manifest_hash", "b" * 64),
        ("t14_configuration_identity", "different-image-config"),
        ("t15_capability_profile_identity", "different-video-capability"),
        ("t14_pipeline_version", "t14/2"),
        ("t15_pipeline_version", "t15/2"),
    ],
)
def test_identity_rejects_material_field_changes_without_new_hash(
    field: str, value: str | int
) -> None:
    payload = make_identity().model_dump(mode="json")
    payload[field] = value
    with pytest.raises(ValidationError, match="identity_hash does not bind"):
        ShotWorkflowIdentity.model_validate(payload)


def test_temporal_contract_is_id_only_and_forbids_payloads() -> None:
    identity = make_identity()
    request = ShotWorkflowInput(
        project_id=PROJECT,
        storyboard_run_id=STORYBOARD,
        storyboard_shot_id=SHOT,
        shot_input_hash=identity.identity_hash,
        workflow_identity=identity,
        idempotency_key="same-input",
    )
    payload = request.model_dump(mode="json")
    assert not ({"prompt", "image_bytes", "video_bytes", "provider_response"} & payload.keys())
    with pytest.raises(ValidationError):
        ShotWorkflowInput.model_validate({**payload, "prompt": "large content"})


def test_activity_keys_are_stable_and_stage_isolated() -> None:
    value = make_identity().identity_hash
    assert shot_activity_idempotency_key(value, "t14") == shot_activity_idempotency_key(
        value, "t14"
    )
    assert shot_activity_idempotency_key(value, "t14") != shot_activity_idempotency_key(
        value, "t15"
    )


def test_ten_shot_failure_isolation_acceptance_model() -> None:
    """Deterministic orchestration model: only failed animation is retried."""
    calls = {index: {"t14": 0, "t15": 0} for index in range(10)}
    states: dict[int, ShotWorkflowStatus] = {}
    for index in range(10):
        calls[index]["t14"] += 1
        calls[index]["t15"] += 1
        states[index] = ShotWorkflowStatus.FAILED if index == 4 else ShotWorkflowStatus.LOCKED
    assert sum(state == ShotWorkflowStatus.LOCKED for state in states.values()) == 9
    calls[4]["t15"] += 1
    states[4] = ShotWorkflowStatus.LOCKED
    assert all(state == ShotWorkflowStatus.LOCKED for state in states.values())
    assert all(calls[index] == {"t14": 1, "t15": 1} for index in range(10) if index != 4)
    assert calls[4] == {"t14": 1, "t15": 2}


@pytest.mark.asyncio
async def test_temporal_ten_shot_concurrency_and_targeted_retry() -> None:
    shots: list[ShotWorkflowInput] = []
    for sequence in range(10):
        shot_id = UUID(int=100 + sequence)
        material: dict[str, str | int] = {
            "project_id": str(PROJECT),
            "storyboard_run_id": str(STORYBOARD),
            "storyboard_input_hash": HASH,
            "storyboard_shot_id": str(shot_id),
            "canonical_shot_hash": f"{sequence:064x}",
            "shot_sequence": sequence,
            "timing_manifest_hash": HASH,
            "t14_configuration_identity": "fake-image/1",
            "t15_capability_profile_identity": "fake-video/1",
            "t14_pipeline_version": "image-generation/1.0.0",
            "t15_pipeline_version": "animation/1.0.0",
            "t16_workflow_version": "t16/1",
            "attempt_policy_version": "shot-attempt/1",
        }
        digest = identity_hash(material)
        identity = ShotWorkflowIdentity(**material, identity_hash=digest)
        shots.append(
            ShotWorkflowInput(
                project_id=PROJECT,
                storyboard_run_id=STORYBOARD,
                storyboard_shot_id=shot_id,
                shot_input_hash=digest,
                workflow_identity=identity,
                idempotency_key=f"acceptance:{sequence}",
            )
        )

    t14_calls = {shot.storyboard_shot_id: 0 for shot in shots}
    t15_calls = {shot.storyboard_shot_id: 0 for shot in shots}
    active = 0
    maximum_active = 0
    all_started = asyncio.Event()
    persisted_checkpoints: dict[UUID, ShotWorkflowProgress] = {}

    async def resolve_fanout(_: ProjectShotFanoutInput) -> ResolveShotFanoutResult:
        return ResolveShotFanoutResult(shots=shots)

    async def resolve_shot(request: ShotWorkflowInput) -> ShotWorkflowProgress:
        return ShotWorkflowProgress(
            state=ShotWorkflowStatus.PROMPTING,
            current_stage="resolved",
            current_attempt=1,
        )

    async def keyframe(request: ShotWorkflowInput) -> ShotWorkflowProgress:
        t14_calls[request.storyboard_shot_id] += 1
        return ShotWorkflowProgress(
            state=ShotWorkflowStatus.KEYFRAME_QA,
            current_stage="t14_complete",
            current_attempt=1,
            t14_run_id=UUID(int=1000 + request.workflow_identity.shot_sequence),
            selected_keyframe_asset_id=UUID(int=2000 + request.workflow_identity.shot_sequence),
        )

    async def animation(request: ShotWorkflowInput) -> ShotWorkflowResult:
        nonlocal active, maximum_active
        shot_id = request.storyboard_shot_id
        t15_calls[shot_id] += 1
        active += 1
        maximum_active = max(maximum_active, active)
        if maximum_active == 10:
            all_started.set()
        await all_started.wait()
        active -= 1
        if request.workflow_identity.shot_sequence == 4 and t15_calls[shot_id] == 1:
            raise ApplicationError(
                "forced polling interruption",
                type="PollingWindowExpired",
                non_retryable=True,
            )
        sequence = request.workflow_identity.shot_sequence
        return ShotWorkflowResult(
            shot_id=shot_id,
            child_workflow_id="activity-placeholder",
            identity_hash=request.shot_input_hash,
            final_state=ShotWorkflowStatus.VIDEO_QA,
            t14_run_id=UUID(int=1000 + sequence),
            selected_keyframe_asset_id=UUID(int=2000 + sequence),
            t15_run_id=UUID(int=3000 + sequence),
            selected_video_asset_id=UUID(int=4000 + sequence),
        )

    async def keyframe_qa(request: ShotWorkflowInput) -> ShotWorkflowProgress:
        """T20 keyframe gate: this fixture's shots all pass."""
        return ShotWorkflowProgress(
            state=ShotWorkflowStatus.KEYFRAME_QA,
            current_stage="t20_keyframe_qa",
            current_attempt=1,
            t14_run_id=UUID(int=1000 + request.workflow_identity.shot_sequence),
            selected_keyframe_asset_id=UUID(int=2000 + request.workflow_identity.shot_sequence),
            last_checkpoint="keyframe_qa_pass",
        )

    async def video_qa(request: ShotWorkflowInput) -> ShotWorkflowProgress:
        """T20 video gate: this fixture's shots all pass."""
        return ShotWorkflowProgress(
            state=ShotWorkflowStatus.VIDEO_QA,
            current_stage="t20_video_qa",
            current_attempt=1,
            last_checkpoint="video_qa_pass",
        )

    async def checkpoint(value: ShotWorkflowProgress) -> ShotWorkflowProgress:
        assert value.selected_keyframe_asset_id is not None
        assert value.selected_video_asset_id is not None
        persisted_checkpoints[value.selected_keyframe_asset_id] = value
        return value

    async def fanout_checkpoint(value: ProjectShotFanoutResult) -> ProjectShotFanoutResult:
        return value

    def named(name: str, fn: Callable[..., Awaitable[object]]) -> Callable[..., Awaitable[object]]:
        return activity.defn(name=name)(fn)

    activities = [
        named("resolve_shot_fanout", resolve_fanout),
        named("resolve_shot_input", resolve_shot),
        named("run_shot_keyframe", keyframe),
        named("run_shot_keyframe_qa", keyframe_qa),
        named("run_shot_animation", animation),
        named("run_shot_video_qa", video_qa),
        named("persist_shot_checkpoint", checkpoint),
        named("persist_shot_fanout_checkpoint", fanout_checkpoint),
    ]
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as environment:
        async with Worker(
            environment.client,
            task_queue="vidgen-projects",
            workflows=[ProjectShotFanoutWorkflow, ShotWorkflow],
            activities=activities,
        ):
            handle = await environment.client.start_workflow(
                ProjectShotFanoutWorkflow.run,
                ProjectShotFanoutInput(
                    project_id=PROJECT,
                    storyboard_run_id=STORYBOARD,
                    idempotency_key="ten-shot-acceptance",
                    concurrency=10,
                ),
                id="ten-shot-acceptance",
                task_queue="vidgen-projects",
            )
            failed = shots[4]
            child: WorkflowHandle[ShotWorkflow, ShotWorkflowResult] = (
                environment.client.get_workflow_handle(
                    temporal_shot_workflow_id(failed.workflow_identity)
                )
            )
            for _ in range(100):
                try:
                    state = await child.query(ShotWorkflow.shot_state)
                except RPCError:
                    await asyncio.sleep(0.01)
                    continue
                if state is not None and state.progress.state == ShotWorkflowStatus.FAILED:
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("forced child did not expose retryable failure")
            for _ in range(100):
                parent_state = await handle.query(ProjectShotFanoutWorkflow.fanout_state)
                if (
                    parent_state is not None
                    and parent_state.locked_count == 9
                    and parent_state.retryable_failure_count == 1
                ):
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("parent did not preserve nine locked sibling results")
            assert maximum_active == 10
            assert sum(value == 1 for value in t15_calls.values()) == 10
            await child.signal(
                ShotWorkflow.command,
                ShotWorkflowCommand(
                    command_id="retry-shot-four",
                    project_id=PROJECT,
                    storyboard_shot_id=failed.storyboard_shot_id,
                    command="retry",
                    expected_state=ShotWorkflowStatus.FAILED,
                ),
            )
            result = await handle.result()
            assert result.status == "shot_generation_complete"
            assert len(persisted_checkpoints) == 10
            assert all(item.t14_run_id is not None for item in persisted_checkpoints.values())
            assert all(item.t15_run_id is not None for item in persisted_checkpoints.values())
            assert result.locked_count == 10
            assert all(value == 1 for value in t14_calls.values())
            assert t15_calls[failed.storyboard_shot_id] == 2
            assert all(
                value == 1
                for shot_id, value in t15_calls.items()
                if shot_id != failed.storyboard_shot_id
            )
            parent_history = await handle.fetch_history()
            child_history = await child.fetch_history()
    await Replayer(
        workflows=[ProjectShotFanoutWorkflow], data_converter=pydantic_data_converter
    ).replay_workflow(parent_history)
    await Replayer(
        workflows=[ShotWorkflow], data_converter=pydantic_data_converter
    ).replay_workflow(child_history)
