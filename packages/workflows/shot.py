"""Replay-safe T16 parent fan-out and per-shot child workflows."""

from __future__ import annotations

from datetime import timedelta
from typing import Literal

from temporalio import workflow
from temporalio.exceptions import CancelledError

with workflow.unsafe.imports_passed_through():
    from packages.workflows.shot_policy import (
        ACTIVITY_TIMEOUT,
        HEARTBEAT_TIMEOUT,
        TASK_QUEUE,
        shot_retry_policy,
        temporal_shot_workflow_id,
    )
    from vidgen.contracts.shot_workflow import (
        ProjectShotFanoutInput,
        ProjectShotFanoutResult,
        ResolveShotFanoutResult,
        ShotFailureClass,
        ShotWorkflowCommand,
        ShotWorkflowCommandResult,
        ShotWorkflowFailure,
        ShotWorkflowInput,
        ShotWorkflowProgress,
        ShotWorkflowQueryResult,
        ShotWorkflowResult,
        ShotWorkflowStatus,
    )


@workflow.defn
class ShotWorkflow:
    def __init__(self) -> None:
        self._request: ShotWorkflowInput | None = None
        self._progress = ShotWorkflowProgress(
            state=ShotWorkflowStatus.DEFINED, current_stage="defined", current_attempt=0
        )
        self._commands: dict[str, ShotWorkflowCommandResult] = {}
        self._cancelled = False

    async def _activity(self, name: str, result_type: type[object]) -> object:
        assert self._request is not None
        return await workflow.execute_activity(
            name,
            self._request,
            result_type=result_type,
            start_to_close_timeout=ACTIVITY_TIMEOUT,
            heartbeat_timeout=HEARTBEAT_TIMEOUT,
            retry_policy=shot_retry_policy(),
        )

    @workflow.run
    async def run(self, request: ShotWorkflowInput) -> ShotWorkflowResult:
        self._request = request
        child_id = temporal_shot_workflow_id(request.workflow_identity)
        try:
            self._progress = ShotWorkflowProgress(
                state=ShotWorkflowStatus.PROMPTING,
                current_stage="resolve_shot_input",
                current_attempt=1,
                started_at=workflow.now(),
                updated_at=workflow.now(),
            )
            self._progress = await self._activity("resolve_shot_input", ShotWorkflowProgress)  # type: ignore[assignment]
            if self._cancelled:
                raise CancelledError()
            self._progress.state = ShotWorkflowStatus.KEYFRAME_GENERATING
            self._progress.current_stage = "t14"
            self._progress = await self._activity("run_shot_keyframe", ShotWorkflowProgress)  # type: ignore[assignment]
            self._progress.state = ShotWorkflowStatus.KEYFRAME_QA
            self._progress.last_checkpoint = "selected_keyframe_persisted"
            if self._cancelled:
                raise CancelledError()
            self._progress.state = ShotWorkflowStatus.ANIMATING
            self._progress.current_stage = "t15"
            result = await self._activity("run_shot_animation", ShotWorkflowResult)
            assert isinstance(result, ShotWorkflowResult)
            self._progress.state = ShotWorkflowStatus.VIDEO_QA
            self._progress.t15_run_id = result.t15_run_id
            self._progress.selected_video_asset_id = result.selected_video_asset_id
            self._progress.state = ShotWorkflowStatus.LOCKED
            self._progress.current_stage = "locked"
            self._progress.last_checkpoint = "authoritative_outputs_locked"
            self._progress.updated_at = workflow.now()
            await workflow.execute_activity(
                "persist_shot_checkpoint",
                self._progress,
                result_type=ShotWorkflowProgress,
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=shot_retry_policy(),
            )
            return result.model_copy(update={"final_state": ShotWorkflowStatus.LOCKED})
        except CancelledError:
            self._progress.state = ShotWorkflowStatus.CANCELLED
            self._progress.current_stage = "cancelled"
            self._progress.last_checkpoint = "cancelled"
            return ShotWorkflowResult(
                shot_id=request.storyboard_shot_id,
                child_workflow_id=child_id,
                identity_hash=request.shot_input_hash,
                final_state=ShotWorkflowStatus.CANCELLED,
                t14_run_id=self._progress.t14_run_id,
                selected_keyframe_asset_id=self._progress.selected_keyframe_asset_id,
            )
        except Exception as exc:
            failure = ShotWorkflowFailure(
                classification=ShotFailureClass.UNKNOWN_FAILURE,
                code=type(exc).__name__,
                retryable=True,
                attempt=max(1, self._progress.current_attempt),
                message=str(exc)[:500],
            )
            self._progress.state = ShotWorkflowStatus.FAILED
            self._progress.retryable = True
            self._progress.last_failure = failure
            return ShotWorkflowResult(
                shot_id=request.storyboard_shot_id,
                child_workflow_id=child_id,
                identity_hash=request.shot_input_hash,
                final_state=ShotWorkflowStatus.FAILED,
                t14_run_id=self._progress.t14_run_id,
                selected_keyframe_asset_id=self._progress.selected_keyframe_asset_id,
                t15_run_id=self._progress.t15_run_id,
                selected_video_asset_id=self._progress.selected_video_asset_id,
                failure=failure,
            )

    @workflow.signal
    async def command(self, command: ShotWorkflowCommand) -> None:
        if command.command_id in self._commands:
            return
        accepted = self._request is not None and command.project_id == self._request.project_id
        request = self._request
        if request is None:
            return
        accepted = accepted and command.storyboard_shot_id == request.storyboard_shot_id
        if command.expected_state is not None:
            accepted = accepted and command.expected_state == self._progress.state
        code = "accepted" if accepted else "stale_or_incompatible"
        if accepted and command.command == "cancel":
            self._cancelled = True
        if accepted and command.command == "regenerate":
            accepted = command.new_shot_input_hash != request.shot_input_hash
            code = "new_identity_required" if not accepted else "start_new_child_identity"
        self._commands[command.command_id] = ShotWorkflowCommandResult(
            command_id=command.command_id,
            accepted=accepted,
            state=self._progress.state,
            code=code,
        )

    @workflow.query
    def shot_state(self) -> ShotWorkflowQueryResult | None:
        if self._request is None:
            return None
        return ShotWorkflowQueryResult(
            workflow_id=temporal_shot_workflow_id(self._request.workflow_identity),
            identity_hash=self._request.shot_input_hash,
            progress=self._progress,
        )

    @workflow.query
    def command_result(self, command_id: str) -> ShotWorkflowCommandResult | None:
        return self._commands.get(command_id)


@workflow.defn
class ProjectShotFanoutWorkflow:
    def __init__(self) -> None:
        self._state: ProjectShotFanoutResult | None = None
        self._cancelled = False
        self._active: list[workflow.ChildWorkflowHandle[ShotWorkflow, ShotWorkflowResult]] = []

    def _aggregate(
        self,
        request: ProjectShotFanoutInput,
        results: list[ShotWorkflowResult],
        total: int,
    ) -> ProjectShotFanoutResult:
        locked = sum(item.final_state == ShotWorkflowStatus.LOCKED for item in results)
        cancelled = sum(item.final_state == ShotWorkflowStatus.CANCELLED for item in results)
        retryable = sum(
            item.final_state == ShotWorkflowStatus.FAILED
            and item.failure is not None
            and item.failure.retryable
            for item in results
        )
        terminal = sum(
            item.final_state == ShotWorkflowStatus.FAILED
            and (item.failure is None or not item.failure.retryable)
            for item in results
        )
        active = len(self._active)
        status: Literal[
            "shot_generation_complete",
            "shot_generation_partial",
            "shot_generation_cancelled",
            "shot_generation_failed",
        ] = "shot_generation_complete" if locked == total else "shot_generation_partial"
        if self._cancelled:
            status = "shot_generation_cancelled"
        elif terminal:
            status = "shot_generation_failed"
        return ProjectShotFanoutResult(
            project_id=request.project_id,
            storyboard_run_id=request.storyboard_run_id,
            status=status,
            results=results,
            total_count=total,
            queued_count=max(0, total - len(results) - active),
            active_count=active,
            locked_count=locked,
            retryable_failure_count=retryable,
            terminal_failure_count=terminal,
            cancelled_count=cancelled,
            current_concurrency=active,
        )

    @workflow.run
    async def run(self, request: ProjectShotFanoutInput) -> ProjectShotFanoutResult:
        resolved = await workflow.execute_activity(
            "resolve_shot_fanout",
            request,
            result_type=ResolveShotFanoutResult,
            start_to_close_timeout=timedelta(minutes=10),
            retry_policy=shot_retry_policy(),
        )
        results: list[ShotWorkflowResult] = []
        shots = resolved.shots
        self._state = self._aggregate(request, results, len(shots))
        for offset in range(0, len(shots), request.concurrency):
            if self._cancelled:
                break
            batch = shots[offset : offset + request.concurrency]
            self._active = [
                await workflow.start_child_workflow(
                    ShotWorkflow.run,
                    shot,
                    id=temporal_shot_workflow_id(shot.workflow_identity),
                    task_queue=TASK_QUEUE,
                    parent_close_policy=workflow.ParentClosePolicy.REQUEST_CANCEL,
                )
                for shot in batch
            ]
            self._state = self._aggregate(request, results, len(shots))
            for child in self._active:
                results.append(await child)
            self._active = []
            self._state = self._aggregate(request, results, len(shots))
        self._state = self._aggregate(request, results, len(shots))
        self._state = await workflow.execute_activity(
            "persist_shot_fanout_checkpoint",
            self._state,
            result_type=ProjectShotFanoutResult,
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=shot_retry_policy(),
        )
        return self._state

    @workflow.signal
    async def cancel_fanout(self) -> None:
        self._cancelled = True
        for child in self._active:
            child.cancel()

    @workflow.query
    def fanout_state(self) -> ProjectShotFanoutResult | None:
        return self._state
