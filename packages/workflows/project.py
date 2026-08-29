from __future__ import annotations

from datetime import timedelta
from uuid import UUID

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from packages.workflows.retry_policies import (
        default_activity_retry_policy,
        provider_activity_retry_policy,
    )
    from packages.workflows.shot import ProjectShotFanoutWorkflow
    from packages.workflows.shot_policy import TASK_QUEUE
    from vidgen.contracts.shot_workflow import ProjectShotFanoutInput, ProjectShotFanoutResult
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

#: The workflow-visible T22 statuses. The project reaches ``completed`` only
#: through ``FINAL_QA_PASSED``; every other terminal final-QA status is a
#: stopping point the UI cannot talk its way past.
FINAL_QA_PASSED = "FINAL_QA_PASSED"
FINAL_QA_REVIEW_REQUIRED = "FINAL_QA_REVIEW_REQUIRED"
FINAL_QA_FAILED = "FINAL_QA_FAILED"
#: Fan-out outcomes that mean every required shot is eligible for assembly.
ELIGIBLE_FANOUT_STATUSES = frozenset({"completed", "shot_generation_complete"})

#: The dedicated render task queue named by the design. Rendering is CPU and
#: disk bound and must not compete with provider-bound activities for the
#: project worker's bounded concurrency.
RENDER_TASK_QUEUE = "render"

#: The workflow-visible T17b statuses, mirroring the durable render-job states.
RENDER_QUEUED = "render_queued"
RENDER_COMPLETE = "render_complete"
RENDER_FAILED = "render_failed"
RENDER_CANCELLED = "render_cancelled"


@workflow.defn
class ProjectWorkflow:
    """The single deterministic owner of a project's T05-T13 execution."""

    def __init__(self) -> None:
        self._state: ProjectWorkflowState | None = None
        self._cancelled = False
        self._render: RenderActivityResult | None = None
        self._final_qa: FinalQAActivityResult | None = None
        self._fanout: (
            workflow.ChildWorkflowHandle[ProjectShotFanoutWorkflow, ProjectShotFanoutResult] | None
        ) = None

    @workflow.run
    async def run(self, request: ProjectWorkflowInput) -> ProjectWorkflowState:
        self._state = ProjectWorkflowState(project_id=request.project_id, status="ingesting")
        stages = (
            ("upload", "run_upload_activity", default_activity_retry_policy()),
            ("media_processing", "run_media_processing_activity", default_activity_retry_policy()),
            (
                "transcript_acquisition",
                "run_transcript_acquisition_activity",
                provider_activity_retry_policy(),
            ),
            ("evidence", "run_evidence_activity", default_activity_retry_policy()),
            ("episode_analysis", "run_episode_analysis_activity", provider_activity_retry_policy()),
            (
                "script_generation",
                "run_script_generation_activity",
                provider_activity_retry_policy(),
            ),
            ("narration", "run_narration_activity", provider_activity_retry_policy()),
            ("storyboard", "run_storyboard_activity", provider_activity_retry_policy()),
        )
        storyboard_id: UUID | None = None
        for stage, activity_name, retry_policy in stages:
            if self._cancelled:
                self._state.cancelled = True
                self._state.status = "cancelled"
                return self._state
            self._state.status = stage
            activity_request = StageActivityInput(
                project_id=request.project_id,
                source_video_id=request.source_video_id,
                stage=stage,
                idempotency_key=f"{request.idempotency_key}:{stage}",
            )
            result = await workflow.execute_activity(
                activity_name,
                activity_request,
                start_to_close_timeout=timedelta(hours=2),
                heartbeat_timeout=timedelta(minutes=2),
                retry_policy=retry_policy,
                result_type=StageActivityResult,
            )
            self._state.completed_stages.append(result.stage)
            if stage == "storyboard":
                storyboard_id = result.entity_id
            if self._cancelled:
                self._state.cancelled = True
                self._state.status = "cancelled"
                return self._state
        if storyboard_id is None:
            raise RuntimeError("storyboard activity did not return the selected storyboard run ID")
        self._state.status = "shot_generation_running"
        self._fanout = await workflow.start_child_workflow(
            ProjectShotFanoutWorkflow.run,
            ProjectShotFanoutInput(
                project_id=request.project_id,
                storyboard_run_id=storyboard_id,
                idempotency_key=f"{request.idempotency_key}:t16",
                trace_context=request.trace_context,
                t15_capability_profile_identity=request.provider_configuration_version,
            ),
            id=f"vidgen-shot-fanout-{request.project_id}-{storyboard_id}",
            task_queue=TASK_QUEUE,
            parent_close_policy=workflow.ParentClosePolicy.REQUEST_CANCEL,
        )
        fanout_result = await self._fanout
        self._state.completed_stages.append("shot_generation")
        self._state.status = fanout_result.status
        if self._cancelled or fanout_result.status not in ELIGIBLE_FANOUT_STATUSES:
            # Final QA inspects an assembled recap. Without every required shot
            # eligible for assembly there is nothing valid to inspect, and
            # running it anyway would only buy a paid analysis of a stale cut.
            return self._state
        render = await self._render_stage(request)
        if render.status != RENDER_COMPLETE or render.final_render_asset_id is None:
            # T22 must never start from an incomplete render. A failed or
            # cancelled render is a stopping point with a structured reason,
            # not a reason to analyse whatever happens to be on disk.
            self._state.status = render.status
            return self._state
        return await self._final_editorial_qa(request)

    async def _render_stage(self, request: ProjectWorkflowInput) -> RenderActivityResult:
        """Render the approved cut, then let T22 inspect exactly that render.

        The message carries IDs only. The activity queues or resumes the render
        job, drives T17b's executor, and returns a bounded result; the manifest,
        the captions, the media and every FFmpeg diagnostic stay in durable
        storage. A retry of this activity resumes the same render job from its
        last durable checkpoint rather than starting a second one.
        """
        assert self._state is not None
        self._state.status = RENDER_QUEUED
        result: RenderActivityResult = await workflow.execute_activity(
            "run_render_activity",
            RenderActivityInput(
                project_id=request.project_id,
                idempotency_key=f"{request.idempotency_key}:t17b",
                trace_context=request.trace_context,
            ),
            # A long encode is bounded by the activity timeout and kept alive by
            # the executor's heartbeats, which also carry cancellation back into
            # FFmpeg rather than leaving a process running after the workflow
            # has moved on.
            start_to_close_timeout=timedelta(hours=6),
            heartbeat_timeout=timedelta(minutes=5),
            retry_policy=default_activity_retry_policy(),
            task_queue=RENDER_TASK_QUEUE,
            result_type=RenderActivityResult,
        )
        self._render = result
        self._state.status = result.status
        if result.status == RENDER_COMPLETE:
            self._state.completed_stages.append("render")
        return result

    async def _final_editorial_qa(self, request: ProjectWorkflowInput) -> ProjectWorkflowState:
        """Run T22 against the project's current T17 render and enforce its gate.

        The message is IDs only. The activity resolves the selected render, its
        manifest and every upstream output from durable storage, and returns
        counts and a decision - never a report, a finding or a frame.
        """
        assert self._state is not None
        self._state.status = "FINAL_QA_QUEUED"
        result = await workflow.execute_activity(
            "run_final_editorial_qa_activity",
            FinalQAActivityInput(
                project_id=request.project_id,
                idempotency_key=f"{request.idempotency_key}:t22",
                trace_context=request.trace_context,
            ),
            start_to_close_timeout=timedelta(hours=2),
            heartbeat_timeout=timedelta(minutes=2),
            retry_policy=provider_activity_retry_policy(),
            result_type=FinalQAActivityResult,
        )
        self._final_qa = result
        self._state.status = result.status
        self._state.completed_stages.append("final_editorial_qa")
        # The completion gate is workflow state, not a UI affordance: only a
        # current PASS advances the project to ``completed``.
        if result.decision == "PASS" and result.status == FINAL_QA_PASSED:
            self._state.status = "completed"
        return self._state

    @workflow.signal
    async def cancel_project(self) -> None:
        self._cancelled = True
        if self._fanout is not None:
            await self._fanout.signal(ProjectShotFanoutWorkflow.cancel_fanout)

    @workflow.query
    def project_state(self) -> ProjectWorkflowState | None:
        return self._state

    @workflow.query
    def render_state(self) -> RenderActivityResult | None:
        """Query-visible T17b progress: IDs, status and percentage only."""
        return self._render

    @workflow.query
    def final_qa_state(self) -> FinalQAActivityResult | None:
        """Query-visible T22 progress: IDs, counts and the current decision."""
        return self._final_qa
