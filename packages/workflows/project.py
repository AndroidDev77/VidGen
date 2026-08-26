from __future__ import annotations

from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from packages.workflows.retry_policies import (
        default_activity_retry_policy,
        provider_activity_retry_policy,
    )
    from vidgen.contracts.workflow import (
        ProjectWorkflowInput,
        ProjectWorkflowState,
        StageActivityInput,
        StageActivityResult,
    )


@workflow.defn
class ProjectWorkflow:
    """The single deterministic owner of a project's T05-T13 execution."""

    def __init__(self) -> None:
        self._state: ProjectWorkflowState | None = None
        self._cancelled = False

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
        for stage, activity_name, retry_policy in stages:
            if self._cancelled:
                self._state.cancelled = True
                self._state.status = "cancelled"
                return self._state
            self._state.status = stage
            result = await workflow.execute_activity(
                activity_name,
                StageActivityInput(
                    project_id=request.project_id,
                    source_video_id=request.source_video_id,
                    stage=stage,
                    idempotency_key=f"{request.idempotency_key}:{stage}",
                ),
                start_to_close_timeout=timedelta(hours=2),
                heartbeat_timeout=timedelta(minutes=2),
                retry_policy=retry_policy,
                result_type=StageActivityResult,
            )
            self._state.completed_stages.append(result.stage)
            if self._cancelled:
                self._state.cancelled = True
                self._state.status = "cancelled"
                return self._state
        self._state.status = "storyboard_complete"
        return self._state

    @workflow.signal
    async def cancel_project(self) -> None:
        self._cancelled = True

    @workflow.query
    def project_state(self) -> ProjectWorkflowState | None:
        return self._state
