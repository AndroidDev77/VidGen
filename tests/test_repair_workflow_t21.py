"""T16 orchestration of the T21 repair, driven against a real Temporal worker.

The workflow only ever exchanges compact, versioned, ID-only messages: prompts,
QA evidence, video bytes, provider responses, fallback manifests and image
payloads are not representable in the contracts it passes.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from pydantic import ValidationError
from temporalio import activity
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from packages.workflows.shot import ShotWorkflow
from packages.workflows.shot_policy import identity_hash, shot_activity_idempotency_key
from vidgen.contracts.shot_workflow import (
    ShotFailureClass,
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
KEYFRAME_ASSET = UUID(int=2001)
ORIGINAL_VIDEO_ASSET = UUID(int=3001)
REPAIRED_VIDEO_ASSET = UUID(int=3002)
REPAIR_RUN = UUID(int=4001)
SELECTED_ATTEMPT = UUID(int=4002)


def _input() -> ShotWorkflowInput:
    material: dict[str, str | int] = {
        "project_id": str(PROJECT),
        "storyboard_run_id": str(STORYBOARD),
        "storyboard_input_hash": HASH,
        "storyboard_shot_id": str(SHOT),
        "canonical_shot_hash": HASH,
        "shot_sequence": 0,
        "timing_manifest_hash": HASH,
        "t14_configuration_identity": "fake-image/1",
        "t15_capability_profile_identity": "fake-video/1",
        "t14_pipeline_version": "image-generation/1.0.0",
        "t15_pipeline_version": "animation/1.0.0",
        "t16_workflow_version": "t16/1",
        "attempt_policy_version": "shot-attempt/1",
    }
    digest = identity_hash(material)
    return ShotWorkflowInput(
        project_id=PROJECT,
        storyboard_run_id=STORYBOARD,
        storyboard_shot_id=SHOT,
        shot_input_hash=digest,
        workflow_identity=ShotWorkflowIdentity(**material, identity_hash=digest),
        idempotency_key="t21-workflow",
    )


def _activities(repair: ShotWorkflowProgress, calls: dict[str, int]) -> list[object]:
    def named(name: str, handler: object) -> object:
        return activity.defn(name=name)(handler)  # type: ignore[arg-type]

    async def resolve_shot(_request: ShotWorkflowInput) -> ShotWorkflowProgress:
        return ShotWorkflowProgress(
            state=ShotWorkflowStatus.PROMPTING, current_stage="resolved", current_attempt=1
        )

    async def keyframe(_request: ShotWorkflowInput) -> ShotWorkflowProgress:
        calls["t14"] += 1
        return ShotWorkflowProgress(
            state=ShotWorkflowStatus.KEYFRAME_QA,
            current_stage="t14_complete",
            current_attempt=1,
            t14_run_id=UUID(int=1001),
            selected_keyframe_asset_id=KEYFRAME_ASSET,
        )

    async def keyframe_qa(_request: ShotWorkflowInput) -> ShotWorkflowProgress:
        return ShotWorkflowProgress(
            state=ShotWorkflowStatus.KEYFRAME_QA,
            current_stage="t20_keyframe_qa",
            current_attempt=1,
        )

    async def animation(request: ShotWorkflowInput) -> ShotWorkflowResult:
        calls["t15"] += 1
        return ShotWorkflowResult(
            shot_id=request.storyboard_shot_id,
            child_workflow_id="child",
            identity_hash=request.shot_input_hash,
            final_state=ShotWorkflowStatus.ANIMATING,
            t14_run_id=UUID(int=1001),
            selected_keyframe_asset_id=KEYFRAME_ASSET,
            t15_run_id=UUID(int=1002),
            selected_video_asset_id=ORIGINAL_VIDEO_ASSET,
        )

    async def video_qa(_request: ShotWorkflowInput) -> ShotWorkflowProgress:
        calls["t20_video"] += 1
        # T20 blocked the clip. T21 owns the recovery; this is not a retry.
        raise ApplicationError("video QA failed", type="VisualQABlocked", non_retryable=True)

    async def shot_repair(_request: ShotWorkflowInput) -> ShotWorkflowProgress:
        calls["t21"] += 1
        return repair

    async def checkpoint(request: ShotWorkflowProgress) -> ShotWorkflowProgress:
        calls["checkpoint"] += 1
        return request

    return [
        named("resolve_shot_input", resolve_shot),
        named("run_shot_keyframe", keyframe),
        named("run_shot_keyframe_qa", keyframe_qa),
        named("run_shot_animation", animation),
        named("run_shot_video_qa", video_qa),
        named("run_shot_repair", shot_repair),
        named("persist_shot_checkpoint", checkpoint),
    ]


async def _run(repair: ShotWorkflowProgress) -> tuple[ShotWorkflowResult, dict[str, int]]:
    calls = {"t14": 0, "t15": 0, "t20_video": 0, "t21": 0, "checkpoint": 0}
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as environment:
        async with Worker(
            environment.client,
            task_queue="vidgen-projects",
            workflows=[ShotWorkflow],
            activities=_activities(repair, calls),  # type: ignore[arg-type]
        ):
            result = await environment.client.execute_workflow(
                ShotWorkflow.run,
                _input(),
                id="t21-repair-workflow",
                task_queue="vidgen-projects",
            )
    return result, calls


@pytest.mark.asyncio
async def test_a_failed_video_qa_starts_t21_and_locks_on_the_repaired_output() -> None:
    repaired = ShotWorkflowProgress(
        state=ShotWorkflowStatus.LOCKED,
        current_stage="t21_repair",
        current_attempt=4,
        selected_video_asset_id=REPAIRED_VIDEO_ASSET,
        repair_run_id=REPAIR_RUN,
        selected_repair_attempt_id=SELECTED_ATTEMPT,
        cost_microusd=600_000,
    )
    result, calls = await _run(repaired)
    assert result.final_state is ShotWorkflowStatus.LOCKED
    # The repaired output, not the original clip, is what the shot locks on.
    assert result.selected_video_asset_id == REPAIRED_VIDEO_ASSET
    assert result.repair_run_id == REPAIR_RUN
    assert result.selected_repair_attempt_id == SELECTED_ATTEMPT
    # T21 ran once for this shot; T14 and T15 were not rerun.
    assert calls == {"t14": 1, "t15": 1, "t20_video": 1, "t21": 1, "checkpoint": 1}


@pytest.mark.asyncio
async def test_an_unrepairable_shot_ends_in_human_review_without_locking() -> None:
    review = ShotWorkflowProgress(
        state=ShotWorkflowStatus.HUMAN_REVIEW_REQUIRED,
        current_stage="t21_repair",
        current_attempt=4,
        repair_run_id=REPAIR_RUN,
        human_review_reason="fallback_ineligible",
    )
    result, calls = await _run(review)
    assert result.final_state is ShotWorkflowStatus.HUMAN_REVIEW_REQUIRED
    assert result.human_review_reason == "fallback_ineligible"
    assert result.failure is not None
    assert result.failure.classification is ShotFailureClass.REPAIR_EXHAUSTED
    assert not result.failure.retryable
    # The shot never reached LOCKED, so nothing was checkpointed as locked.
    assert calls["checkpoint"] == 0
    assert calls["t21"] == 1


def test_the_repair_workflow_contract_is_id_only() -> None:
    progress = ShotWorkflowProgress(
        state=ShotWorkflowStatus.REPAIRING,
        current_stage="t21_repair",
        current_attempt=1,
        repair_run_id=REPAIR_RUN,
    )
    payload = progress.model_dump(mode="json")
    forbidden = {
        "prompt",
        "prompt_delta",
        "qa_evidence",
        "video_bytes",
        "image_bytes",
        "provider_response",
        "fallback_manifest",
    }
    assert not (forbidden & payload.keys())
    for field in sorted(forbidden):
        with pytest.raises(ValidationError):
            ShotWorkflowProgress.model_validate({**payload, field: "large content"})


def test_the_repair_activity_key_is_stable_and_stage_isolated() -> None:
    value = _input().shot_input_hash
    assert shot_activity_idempotency_key(value, "t21") == shot_activity_idempotency_key(
        value, "t21"
    )
    assert shot_activity_idempotency_key(value, "t21") != shot_activity_idempotency_key(
        value, "t20-video"
    )


def test_every_t21_state_is_representable_in_the_shot_workflow() -> None:
    from vidgen.contracts.repair import RepairRunState

    states = {item.value for item in ShotWorkflowStatus}
    assert {item.value.lower() for item in RepairRunState} <= states
