"""Replay-safe T16 parent fan-out and per-shot child workflows."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Literal
from uuid import UUID

from temporalio import workflow
from temporalio.exceptions import ActivityError, CancelledError

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
        self._retry_requested = False

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
        while not self._cancelled:
            try:
                return await self._generate(request, child_id)
            except CancelledError:
                self._cancelled = True
                break
            except Exception as exc:
                failure = self._classify_failure(exc)
                self._progress.state = ShotWorkflowStatus.FAILED
                self._progress.current_stage = "failed"
                self._progress.retryable = failure.retryable
                self._progress.last_failure = failure
                self._progress.updated_at = workflow.now()
                await self._report(self._failure_result(request, child_id, failure))
                if not failure.retryable:
                    return self._failure_result(request, child_id, failure)
                await workflow.wait_condition(lambda: self._retry_requested or self._cancelled)
                if self._cancelled:
                    break
                self._retry_requested = False
                self._progress.current_attempt += 1
                self._progress.retryable = False
                self._progress.last_failure = None
        self._progress.state = ShotWorkflowStatus.CANCELLED
        self._progress.current_stage = "cancelled"
        self._progress.last_checkpoint = "cancelled"
        result = ShotWorkflowResult(
            shot_id=request.storyboard_shot_id,
            child_workflow_id=child_id,
            identity_hash=request.shot_input_hash,
            final_state=ShotWorkflowStatus.CANCELLED,
            t14_run_id=self._progress.t14_run_id,
            selected_keyframe_asset_id=self._progress.selected_keyframe_asset_id,
            t15_run_id=self._progress.t15_run_id,
            selected_video_asset_id=self._progress.selected_video_asset_id,
        )
        await self._report(result)
        return result

    async def _generate(self, request: ShotWorkflowInput, child_id: str) -> ShotWorkflowResult:
        if self._progress.current_attempt == 0:
            self._progress = ShotWorkflowProgress(
                state=ShotWorkflowStatus.PROMPTING,
                current_stage="resolve_shot_input",
                current_attempt=1,
                started_at=workflow.now(),
                updated_at=workflow.now(),
            )
            self._progress = await self._activity("resolve_shot_input", ShotWorkflowProgress)  # type: ignore[assignment]
            self._progress.current_attempt = max(1, self._progress.current_attempt)
            if self._cancelled:
                raise CancelledError()
        if self._progress.selected_keyframe_asset_id is None:
            self._progress.state = ShotWorkflowStatus.KEYFRAME_GENERATING
            self._progress.current_stage = "t14"
            self._progress = await self._activity("run_shot_keyframe", ShotWorkflowProgress)  # type: ignore[assignment]
            self._progress.last_checkpoint = "selected_keyframe_persisted"
            if self._cancelled:
                raise CancelledError()
        # A keyframe must pass T20 before any animation spend.
        self._progress.state = ShotWorkflowStatus.KEYFRAME_QA
        self._progress.current_stage = "t20_keyframe_qa"
        t14_run_id = self._progress.t14_run_id
        selected_keyframe_asset_id = self._progress.selected_keyframe_asset_id
        self._progress = await self._activity("run_shot_keyframe_qa", ShotWorkflowProgress)  # type: ignore[assignment]
        # QA activities intentionally return only compact QA status. Preserve
        # the durable T14 checkpoint so a later T15/T20 failure still reports
        # the selected keyframe and can resume without regenerating it.
        self._progress.t14_run_id = t14_run_id
        self._progress.selected_keyframe_asset_id = selected_keyframe_asset_id
        if self._cancelled:
            raise CancelledError()
        self._progress.state = ShotWorkflowStatus.ANIMATING
        self._progress.current_stage = "t15"
        result = await self._activity("run_shot_animation", ShotWorkflowResult)
        assert isinstance(result, ShotWorkflowResult)
        if self._cancelled:
            raise CancelledError()
        # A canonical clip must pass T20 before the shot can lock.
        self._progress.state = ShotWorkflowStatus.VIDEO_QA
        self._progress.current_stage = "t20_video_qa"
        self._progress.t15_run_id = result.t15_run_id
        self._progress.selected_video_asset_id = result.selected_video_asset_id
        self._progress = await self._activity("run_shot_video_qa", ShotWorkflowProgress)  # type: ignore[assignment]
        self._progress.t14_run_id = result.t14_run_id
        self._progress.selected_keyframe_asset_id = result.selected_keyframe_asset_id
        self._progress.t15_run_id = result.t15_run_id
        self._progress.selected_video_asset_id = result.selected_video_asset_id
        if self._cancelled:
            raise CancelledError()
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
        locked = result.model_copy(
            update={
                "child_workflow_id": child_id,
                "final_state": ShotWorkflowStatus.LOCKED,
            }
        )
        await self._report(locked)
        return locked

    async def _report(self, result: ShotWorkflowResult) -> None:
        if self._request is None or self._request.parent_workflow_id is None:
            return
        parent = workflow.get_external_workflow_handle(self._request.parent_workflow_id)
        await parent.signal(ProjectShotFanoutWorkflow.shot_progress, result)

    def _classify_failure(self, exc: Exception) -> ShotWorkflowFailure:
        cause = exc.cause if isinstance(exc, ActivityError) and exc.cause is not None else exc
        name = getattr(cause, "type", None) or type(cause).__name__
        mapping = {
            "InvalidLineage": (ShotFailureClass.INVALID_LINEAGE, False),
            "ImageGenerationLineageError": (ShotFailureClass.INVALID_LINEAGE, False),
            "DeterministicConfigurationFailure": (
                ShotFailureClass.DETERMINISTIC_CONFIGURATION_FAILURE,
                False,
            ),
            "BudgetDenied": (ShotFailureClass.BUDGET_DENIAL, False),
            # T20 outcomes: a blocked shot waits for T21 repair, and a
            # review-required shot waits for a human decision. Neither is
            # retried automatically, and neither reruns T14 or T15.
            "VisualQABlocked": (ShotFailureClass.VISUAL_QA_FAILURE, False),
            "VisualQAReviewRequired": (ShotFailureClass.VISUAL_QA_REVIEW_REQUIRED, False),
            "VisualQALineageError": (ShotFailureClass.INVALID_LINEAGE, False),
            "BudgetExceededError": (ShotFailureClass.BUDGET_DENIAL, False),
            "UnsupportedCapability": (ShotFailureClass.UNSUPPORTED_CAPABILITY, False),
            "UnknownProviderOutcome": (ShotFailureClass.UNKNOWN_FAILURE, False),
            "AmbiguousVideoSubmission": (ShotFailureClass.UNKNOWN_FAILURE, False),
            "PollingWindowExpired": (ShotFailureClass.POLLING_INTERRUPTION, True),
            "RateLimitError": (ShotFailureClass.RATE_LIMIT, True),
            "TimeoutError": (ShotFailureClass.PROVIDER_TIMEOUT, True),
        }
        classification, retryable = mapping.get(
            name, (ShotFailureClass.TRANSIENT_PROVIDER_FAILURE, True)
        )
        return ShotWorkflowFailure(
            classification=classification,
            code=str(name)[:100],
            retryable=retryable,
            attempt=max(1, self._progress.current_attempt),
            message=str(cause)[:500],
        )

    def _failure_result(
        self, request: ShotWorkflowInput, child_id: str, failure: ShotWorkflowFailure
    ) -> ShotWorkflowResult:
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
        if accepted and command.command in {"retry", "resume"}:
            accepted = (
                self._progress.state == ShotWorkflowStatus.FAILED and self._progress.retryable
            )
            code = "retry_scheduled" if accepted else "shot_not_retryable"
            self._retry_requested = accepted
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
        self._reported: dict[UUID, ShotWorkflowResult] = {}

    def _aggregate(
        self,
        request: ProjectShotFanoutInput,
        results: list[ShotWorkflowResult],
        total: int,
    ) -> ProjectShotFanoutResult:
        merged = {item.shot_id: item for item in results}
        merged.update(self._reported)
        compact_results = list(merged.values())
        locked = sum(item.final_state == ShotWorkflowStatus.LOCKED for item in compact_results)
        cancelled = sum(
            item.final_state == ShotWorkflowStatus.CANCELLED for item in compact_results
        )
        retryable = sum(
            item.final_state == ShotWorkflowStatus.FAILED
            and item.failure is not None
            and item.failure.retryable
            for item in compact_results
        )
        terminal = sum(
            item.final_state == ShotWorkflowStatus.FAILED
            and (item.failure is None or not item.failure.retryable)
            for item in compact_results
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
            results=compact_results,
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
        next_shot = 0
        pending: dict[
            asyncio.Future[ShotWorkflowResult],
            workflow.ChildWorkflowHandle[ShotWorkflow, ShotWorkflowResult],
        ] = {}
        while (next_shot < len(shots) or pending) and not self._cancelled:
            while next_shot < len(shots) and len(pending) < request.concurrency:
                shot = shots[next_shot].model_copy(
                    update={"parent_workflow_id": workflow.info().workflow_id}
                )
                child = await workflow.start_child_workflow(
                    ShotWorkflow.run,
                    shot,
                    id=temporal_shot_workflow_id(shot.workflow_identity),
                    task_queue=TASK_QUEUE,
                    parent_close_policy=workflow.ParentClosePolicy.REQUEST_CANCEL,
                )
                self._active.append(child)
                pending[asyncio.ensure_future(child)] = child
                next_shot += 1
            self._state = self._aggregate(request, results, len(shots))
            completed, _ = await workflow.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for future in completed:
                results.append(future.result())
                child = pending.pop(future)
                self._active.remove(child)
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

    @workflow.signal
    async def shot_progress(self, result: ShotWorkflowResult) -> None:
        self._reported[result.shot_id] = result

    @workflow.query
    def fanout_state(self) -> ProjectShotFanoutResult | None:
        return self._state
