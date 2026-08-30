"""T18b acceptance: the whole control-plane lifecycle against a real Temporal.

These tests run the real workflows on a Temporal test environment with the
deterministic fake activity handlers. Nothing here uses the fake workflow
controller or a frontend mock, because the point is exactly what those cannot
prove: that a durable command reaches a real workflow, that a human pause is
genuinely durable, that an approval resumes it, and that a paused project can be
continued by a new generation run rather than by re-entering a closed execution.

No provider is called and no cost is incurred.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from uuid import UUID, uuid4

import pytest
from temporalio import activity
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from packages.workflows.continuity import ContinuityReferenceWorkflow
from packages.workflows.control import FinalEditorialQAWorkflow, RenderWorkflow
from packages.workflows.project import RENDER_TASK_QUEUE, ProjectWorkflow
from packages.workflows.shot import ProjectShotFanoutWorkflow, ShotWorkflow
from packages.workflows.shot_policy import TASK_QUEUE
from vidgen.contracts.continuity_workflow import (
    ReferenceApprovalSignal,
    ReferenceDraftResult,
    ReferenceWorkflowInput,
    ReferenceWorkflowResult,
    ReferenceWorkflowStatus,
)
from vidgen.contracts.shot_workflow import ProjectShotFanoutResult, ResolveShotFanoutResult
from vidgen.contracts.workflow import (
    FinalQAActivityInput,
    FinalQAActivityResult,
    ProjectWorkflowInput,
    ProjectWorkflowState,
    RenderActivityInput,
    RenderActivityResult,
    StageActivityInput,
    StageActivityResult,
)

PROJECT = UUID("00000000-0000-0000-0000-0000000018b0")
SOURCE = UUID("00000000-0000-0000-0000-0000000018b1")
ANALYSIS = UUID("00000000-0000-0000-0000-0000000018b2")
STORYBOARD = UUID("00000000-0000-0000-0000-0000000018b3")
REFERENCE_RUN = UUID("00000000-0000-0000-0000-0000000018b4")
RENDER_JOB = UUID("00000000-0000-0000-0000-0000000018b5")
RENDER_ASSET = UUID("00000000-0000-0000-0000-0000000018b6")
MANIFEST_ASSET = UUID("00000000-0000-0000-0000-0000000018b7")
FINAL_QA_RUN = UUID("00000000-0000-0000-0000-0000000018b8")

STAGES = (
    "upload",
    "media_processing",
    "transcript_acquisition",
    "evidence",
    "episode_analysis",
    "script_generation",
    "narration",
    "storyboard",
)

Activities = list[Callable[..., Awaitable[object]]]


def _stage_activities(executed: list[str]) -> Activities:
    """One stub per T05-T13 stage, recording which ones actually ran."""

    def make(stage: str) -> Callable[..., Awaitable[object]]:
        async def handler(request: StageActivityInput) -> StageActivityResult:
            executed.append(request.stage)
            return StageActivityResult(stage=request.stage)

        return activity.defn(name=f"run_{stage}_activity")(handler)

    return [make(stage) for stage in STAGES]


def _continuity_activities(
    *,
    requires_approval: bool,
    approvals: list[ReferenceApprovalSignal],
    affected_shot_ids: list[UUID] | None = None,
) -> Activities:
    async def resolve(request: StageActivityInput) -> ReferenceWorkflowInput:
        return ReferenceWorkflowInput(
            project_id=request.project_id,
            episode_analysis_id=ANALYSIS,
            storyboard_run_id=STORYBOARD,
            reference_run_id=REFERENCE_RUN,
            idempotency_key="t19",
        )

    async def build(request: ReferenceWorkflowInput) -> ReferenceDraftResult:
        return ReferenceDraftResult(
            project_id=request.project_id,
            reference_run_id=request.reference_run_id,
            draft_version_ids=[uuid4()] if requires_approval else [],
            entity_count=1 if requires_approval else 0,
            requires_approval=requires_approval,
            status=("references_awaiting_approval" if requires_approval else "references_complete"),
        )

    async def apply(signal: ReferenceApprovalSignal) -> ReferenceWorkflowResult:
        approvals.append(signal)
        return ReferenceWorkflowResult(
            project_id=signal.project_id,
            reference_run_id=signal.reference_run_id,
            status=ReferenceWorkflowStatus.COMPLETE,
            approved_version_ids=list(signal.approved_reference_set_ids),
            affected_shot_ids=list(affected_shot_ids or []),
        )

    return [
        activity.defn(name="resolve_continuity_inputs")(resolve),
        activity.defn(name="build_continuity_references")(build),
        activity.defn(name="apply_continuity_references")(apply),
    ]


def _fanout_activities() -> Activities:
    async def resolve_fanout(request: object) -> ResolveShotFanoutResult:
        return ResolveShotFanoutResult(shots=[])

    async def checkpoint(value: ProjectShotFanoutResult) -> ProjectShotFanoutResult:
        return value

    return [
        activity.defn(name="resolve_shot_fanout")(resolve_fanout),
        activity.defn(name="persist_shot_fanout_checkpoint")(checkpoint),
    ]


def _final_qa_activity(seen: list[FinalQAActivityInput], *, decision: str = "PASS") -> Activities:
    async def handler(request: FinalQAActivityInput) -> FinalQAActivityResult:
        seen.append(request)
        return FinalQAActivityResult(
            project_id=request.project_id,
            final_editorial_run_id=FINAL_QA_RUN,
            final_render_asset_id=request.final_render_asset_id or RENDER_ASSET,
            status="FINAL_QA_PASSED" if decision == "PASS" else "FINAL_QA_REVIEW_REQUIRED",
            phase="COMPLETION_GATE",
            decision=decision,  # type: ignore[arg-type]
        )

    return [activity.defn(name="run_final_editorial_qa_activity")(handler)]


def _render_activity(seen: list[RenderActivityInput]) -> Activities:
    async def handler(request: RenderActivityInput) -> RenderActivityResult:
        seen.append(request)
        return RenderActivityResult(
            project_id=request.project_id,
            render_job_id=RENDER_JOB,
            status="render_complete",
            progress_percent=100,
            final_render_asset_id=RENDER_ASSET,
            render_manifest_asset_id=MANIFEST_ASSET,
        )

    return [activity.defn(name="run_render_activity")(handler)]


WORKFLOWS = [
    ProjectWorkflow,
    ProjectShotFanoutWorkflow,
    ShotWorkflow,
    ContinuityReferenceWorkflow,
    FinalEditorialQAWorkflow,
    RenderWorkflow,
]


async def _environment(
    activities: Activities, render_activities: Activities
) -> tuple[WorkflowEnvironment, Worker, Worker]:
    environment = await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    )
    project_worker = Worker(
        environment.client, task_queue=TASK_QUEUE, workflows=WORKFLOWS, activities=activities
    )
    render_worker = Worker(
        environment.client, task_queue=RENDER_TASK_QUEUE, activities=render_activities
    )
    return environment, project_worker, render_worker


def _input(
    *, entry_stage: str = "upload", generation_run_id: UUID | None = None
) -> ProjectWorkflowInput:
    return ProjectWorkflowInput(
        project_id=PROJECT,
        source_video_id=SOURCE,
        idempotency_key=f"t18b-{entry_stage}",
        entry_stage=entry_stage,
        generation_run_id=generation_run_id,
    )


@pytest.mark.asyncio
async def test_a_project_with_no_references_completes_without_a_human_decision() -> None:
    """T19 must not stall a project that has nothing to reference."""
    executed: list[str] = []
    approvals: list[ReferenceApprovalSignal] = []
    final_qa: list[FinalQAActivityInput] = []
    renders: list[RenderActivityInput] = []
    activities = [
        *_stage_activities(executed),
        *_continuity_activities(requires_approval=False, approvals=approvals),
        *_fanout_activities(),
        *_final_qa_activity(final_qa),
    ]
    environment, project_worker, render_worker = await _environment(
        activities, _render_activity(renders)
    )
    async with environment, project_worker, render_worker:
        state = await environment.client.execute_workflow(
            ProjectWorkflow.run,
            _input(),
            id=f"project-{uuid4()}",
            task_queue=TASK_QUEUE,
        )
    assert state.status == "completed"
    assert "continuity_references" in state.completed_stages
    # The binding still ran: a shot with no references gets an explicit empty
    # bundle rather than silently taking the legacy no-reference path.
    assert len(approvals) == 1
    assert approvals[0].reference_run_id == REFERENCE_RUN
    assert executed == list(STAGES)
    assert len(renders) == 1 and len(final_qa) == 1


@pytest.mark.asyncio
async def test_references_pause_the_project_and_an_approval_resumes_it() -> None:
    """The pause is durable and queryable, and the approval reaches the child."""
    approvals: list[ReferenceApprovalSignal] = []
    final_qa: list[FinalQAActivityInput] = []
    activities = [
        *_stage_activities([]),
        *_continuity_activities(requires_approval=True, approvals=approvals),
        *_fanout_activities(),
        *_final_qa_activity(final_qa),
    ]
    environment, project_worker, render_worker = await _environment(
        activities, _render_activity([])
    )
    async with environment, project_worker, render_worker:
        handle = await environment.client.start_workflow(
            ProjectWorkflow.run,
            _input(),
            id=f"project-{uuid4()}",
            task_queue=TASK_QUEUE,
        )
        reference_handle = environment.client.get_workflow_handle(
            f"vidgen-references-{REFERENCE_RUN}"
        )
        await _await_reference_status(reference_handle, ReferenceWorkflowStatus.AWAITING_APPROVAL)
        # The parent says it is waiting, and says why, rather than showing a
        # generic "running" while nothing progresses.
        parent = await _await_waiting(handle)
        assert parent.waiting_reason == "reference_approval_required"
        assert parent.next_actions == ["approve_references"]
        assert final_qa == [], "no paid analysis may start while references are unbound"

        approved = uuid4()
        await reference_handle.signal(
            ContinuityReferenceWorkflow.approve,
            ReferenceApprovalSignal(
                project_id=PROJECT,
                reference_run_id=REFERENCE_RUN,
                approval_id=uuid4(),
                idempotency_key="approval-1",
                storyboard_run_id=STORYBOARD,
                approved_reference_set_ids=[approved],
            ),
        )
        state = await handle.result()
        history = await handle.fetch_history()
        reference_history = await reference_handle.fetch_history()
    assert state.status == "completed"
    assert [signal.approved_reference_set_ids for signal in approvals] == [[approved]]
    # Replay both: the pause and the resume must be deterministic.
    await Replayer(workflows=WORKFLOWS, data_converter=pydantic_data_converter).replay_workflow(
        history
    )
    await Replayer(workflows=WORKFLOWS, data_converter=pydantic_data_converter).replay_workflow(
        reference_history
    )


@pytest.mark.asyncio
async def test_a_duplicate_approval_is_ignored_by_the_waiting_workflow() -> None:
    """A double-submitted approval must not bind twice."""
    approvals: list[ReferenceApprovalSignal] = []
    activities = [
        *_stage_activities([]),
        *_continuity_activities(requires_approval=True, approvals=approvals),
        *_fanout_activities(),
        *_final_qa_activity([]),
    ]
    environment, project_worker, render_worker = await _environment(
        activities, _render_activity([])
    )
    async with environment, project_worker, render_worker:
        handle = await environment.client.start_workflow(
            ProjectWorkflow.run, _input(), id=f"project-{uuid4()}", task_queue=TASK_QUEUE
        )
        reference_handle = environment.client.get_workflow_handle(
            f"vidgen-references-{REFERENCE_RUN}"
        )
        await _await_reference_status(reference_handle, ReferenceWorkflowStatus.AWAITING_APPROVAL)
        signal = ReferenceApprovalSignal(
            project_id=PROJECT,
            reference_run_id=REFERENCE_RUN,
            approval_id=uuid4(),
            idempotency_key="approval-1",
            storyboard_run_id=STORYBOARD,
        )
        await reference_handle.signal(ContinuityReferenceWorkflow.approve, signal)
        await reference_handle.signal(ContinuityReferenceWorkflow.approve, signal)
        await handle.result()
    assert len(approvals) == 1


@pytest.mark.asyncio
async def test_a_continuation_run_reuses_every_stage_above_its_entry_point() -> None:
    """A revision must not pay again for work its change cannot have touched."""
    executed: list[str] = []
    final_qa: list[FinalQAActivityInput] = []
    renders: list[RenderActivityInput] = []
    generation_run = uuid4()
    activities = [
        *_stage_activities(executed),
        *_continuity_activities(requires_approval=False, approvals=[]),
        *_fanout_activities(),
        *_final_qa_activity(final_qa),
    ]
    environment, project_worker, render_worker = await _environment(
        activities, _render_activity(renders)
    )
    async with environment, project_worker, render_worker:
        state = await environment.client.execute_workflow(
            ProjectWorkflow.run,
            _input(entry_stage="narration", generation_run_id=generation_run),
            id=f"project-{uuid4()}",
            task_queue=TASK_QUEUE,
        )
    # A script revision rebuilds from narration; the transcript and the episode
    # analysis above it are reused untouched.
    assert executed == ["narration", "storyboard"]
    assert state.generation_run_id == generation_run
    assert state.entry_stage == "narration"
    assert state.status == "completed"
    assert len(renders) == 1


@pytest.mark.asyncio
async def test_a_render_entry_stage_skips_straight_to_final_qa() -> None:
    """Continuing after a remediation must not regenerate a single shot."""
    executed: list[str] = []
    final_qa: list[FinalQAActivityInput] = []
    activities = [
        *_stage_activities(executed),
        *_continuity_activities(requires_approval=False, approvals=[]),
        *_fanout_activities(),
        *_final_qa_activity(final_qa),
    ]
    environment, project_worker, render_worker = await _environment(
        activities, _render_activity([])
    )
    async with environment, project_worker, render_worker:
        state = await environment.client.execute_workflow(
            ProjectWorkflow.run,
            _input(entry_stage="final_editorial_qa"),
            id=f"project-{uuid4()}",
            task_queue=TASK_QUEUE,
        )
    assert executed == []
    assert len(final_qa) == 1
    assert state.status == "completed"


@pytest.mark.asyncio
async def test_a_review_required_gate_leaves_the_project_short_of_completed() -> None:
    """The gate is workflow state, and a review is a stopping point, not a pass."""
    final_qa: list[FinalQAActivityInput] = []
    activities = [
        *_stage_activities([]),
        *_continuity_activities(requires_approval=False, approvals=[]),
        *_fanout_activities(),
        *_final_qa_activity(final_qa, decision="REVIEW"),
    ]
    environment, project_worker, render_worker = await _environment(
        activities, _render_activity([])
    )
    async with environment, project_worker, render_worker:
        state = await environment.client.execute_workflow(
            ProjectWorkflow.run, _input(), id=f"project-{uuid4()}", task_queue=TASK_QUEUE
        )
    assert state.status == "FINAL_QA_REVIEW_REQUIRED"
    assert state.waiting_reason == "final_qa_review_required"
    assert "continue_project" in state.next_actions


@pytest.mark.asyncio
async def test_the_manual_final_qa_workflow_runs_the_canonical_activity() -> None:
    """A manual T22 run is a real workflow, and it inspects the render it names."""
    final_qa: list[FinalQAActivityInput] = []
    environment, project_worker, render_worker = await _environment(
        _final_qa_activity(final_qa), _render_activity([])
    )
    async with environment, project_worker, render_worker:
        result = await environment.client.execute_workflow(
            FinalEditorialQAWorkflow.run,
            FinalQAActivityInput(
                project_id=PROJECT,
                final_render_asset_id=RENDER_ASSET,
                render_manifest_asset_id=MANIFEST_ASSET,
                idempotency_key="manual-t22",
            ),
            id=f"final-qa-{uuid4()}",
            task_queue=TASK_QUEUE,
        )
    assert result.decision == "PASS"
    assert [item.final_render_asset_id for item in final_qa] == [RENDER_ASSET]


@pytest.mark.asyncio
async def test_the_render_workflow_drives_the_canonical_executor_on_its_own_queue() -> None:
    """A requested rerender must not compete with provider-bound activities."""
    renders: list[RenderActivityInput] = []
    environment, project_worker, render_worker = await _environment([], _render_activity(renders))
    async with environment, project_worker, render_worker:
        result = await environment.client.execute_workflow(
            RenderWorkflow.run,
            RenderActivityInput(
                project_id=PROJECT, render_job_id=RENDER_JOB, idempotency_key="rerender"
            ),
            id=f"render-{uuid4()}",
            task_queue=TASK_QUEUE,
        )
    assert result.status == "render_complete"
    assert [item.render_job_id for item in renders] == [RENDER_JOB]


@pytest.mark.asyncio
async def test_cancelling_a_project_stops_the_reference_child_it_owns() -> None:
    """Parent cancellation must reach a child that is durably waiting."""
    activities = [
        *_stage_activities([]),
        *_continuity_activities(requires_approval=True, approvals=[]),
        *_fanout_activities(),
        *_final_qa_activity([]),
    ]
    environment, project_worker, render_worker = await _environment(
        activities, _render_activity([])
    )
    async with environment, project_worker, render_worker:
        handle = await environment.client.start_workflow(
            ProjectWorkflow.run, _input(), id=f"project-{uuid4()}", task_queue=TASK_QUEUE
        )
        reference_handle = environment.client.get_workflow_handle(
            f"vidgen-references-{REFERENCE_RUN}"
        )
        await _await_reference_status(reference_handle, ReferenceWorkflowStatus.AWAITING_APPROVAL)
        await handle.signal(ProjectWorkflow.cancel_project)
        state = await handle.result()
    assert state.cancelled is True
    assert state.status == "cancelled"


async def _await_reference_status(
    handle: object, expected: ReferenceWorkflowStatus, attempts: int = 200
) -> None:
    for _ in range(attempts):
        try:
            status = await handle.query(ContinuityReferenceWorkflow.status)  # type: ignore[attr-defined]
        except Exception:
            status = None
        if status is expected:
            return
        await asyncio.sleep(0.02)
    pytest.fail(f"the reference workflow never reached {expected}")


async def _await_waiting(handle: object, attempts: int = 200) -> ProjectWorkflowState:
    for _ in range(attempts):
        state = await handle.query(ProjectWorkflow.project_state)  # type: ignore[attr-defined]
        if state is not None and state.waiting_reason:
            return state
        await asyncio.sleep(0.02)
    pytest.fail("the project workflow never reported a durable waiting state")


def test_the_client_module_is_importable_without_a_cluster() -> None:
    """A guard: importing the workflows must not require a running Temporal."""
    assert Client is not None
