"""Durable, replay-safe T19 drafting and human approval workflow."""

from __future__ import annotations

from datetime import timedelta
from typing import cast

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from packages.workflows.retry_policies import default_activity_retry_policy
    from vidgen.contracts.continuity_workflow import (
        ReferenceApprovalSignal,
        ReferenceDraftResult,
        ReferenceWorkflowInput,
        ReferenceWorkflowResult,
        ReferenceWorkflowStatus,
    )


@workflow.defn
class ContinuityReferenceWorkflow:
    """Build drafts once, wait durably, then bind only an approved lineage."""

    def __init__(self) -> None:
        self._request: ReferenceWorkflowInput | None = None
        self._status = ReferenceWorkflowStatus.QUEUED
        self._approval: ReferenceApprovalSignal | None = None
        self._drafts: ReferenceDraftResult | None = None
        self._seen_approval_keys: set[str] = set()
        self._cancelled = False

    @workflow.run
    async def run(self, request: ReferenceWorkflowInput) -> ReferenceWorkflowResult:
        self._request = request
        self._status = ReferenceWorkflowStatus.SELECTING
        drafts = cast(
            ReferenceDraftResult,
            await workflow.execute_activity(
                "build_continuity_references",
                request,
                result_type=ReferenceDraftResult,
                start_to_close_timeout=timedelta(hours=2),
                heartbeat_timeout=timedelta(minutes=2),
                retry_policy=default_activity_retry_policy(),
            ),
        )
        self._drafts = drafts
        if self._cancelled:
            return self._cancelled_result(request)
        if not drafts.requires_approval:
            # Nothing in this project needs a reference sheet, so there is no
            # decision to wait for. Completing here is what keeps a project
            # without characters or locations from stalling forever - and the
            # binding still runs, so a shot with no references gets an explicit
            # empty bundle rather than the legacy no-reference path.
            self._approval = ReferenceApprovalSignal(
                project_id=request.project_id,
                reference_run_id=request.reference_run_id,
                approval_id=request.reference_run_id,
                idempotency_key=f"{request.idempotency_key}:no-references",
                storyboard_run_id=request.storyboard_run_id,
            )
        else:
            self._status = ReferenceWorkflowStatus.AWAITING_APPROVAL
            await self._report_waiting(request)
            await workflow.wait_condition(lambda: self._approval is not None or self._cancelled)
        if self._cancelled:
            return self._cancelled_result(request)
        assert self._approval is not None
        self._status = ReferenceWorkflowStatus.BINDING
        result = cast(
            ReferenceWorkflowResult,
            await workflow.execute_activity(
                "apply_continuity_references",
                self._approval,
                result_type=ReferenceWorkflowResult,
                start_to_close_timeout=timedelta(hours=2),
                heartbeat_timeout=timedelta(minutes=2),
                retry_policy=default_activity_retry_policy(),
            ),
        )
        self._status = result.status
        return result

    async def _report_waiting(self, request: ReferenceWorkflowInput) -> None:
        """Tell the owning project workflow that a human decision is now owed.

        Signalled by name rather than by class so T19 never imports the parent
        workflow, which imports T19. The parent renders the pause; nothing about
        the references themselves crosses the signal.
        """
        if request.parent_workflow_id is None:
            return
        parent = workflow.get_external_workflow_handle(request.parent_workflow_id)
        await parent.signal("reference_progress", self._status.value)

    def _cancelled_result(self, request: ReferenceWorkflowInput) -> ReferenceWorkflowResult:
        self._status = ReferenceWorkflowStatus.CANCELLED
        return ReferenceWorkflowResult(
            project_id=request.project_id,
            reference_run_id=request.reference_run_id,
            status=self._status,
            cancelled=True,
        )

    @workflow.signal
    async def approve(self, signal: ReferenceApprovalSignal) -> None:
        if self._request is None or signal.idempotency_key in self._seen_approval_keys:
            return
        if (signal.project_id, signal.reference_run_id) != (
            self._request.project_id,
            self._request.reference_run_id,
        ):
            return
        self._seen_approval_keys.add(signal.idempotency_key)
        if self._approval is None:
            self._approval = signal

    @workflow.signal
    async def cancel(self) -> None:
        self._cancelled = True

    @workflow.query
    def status(self) -> ReferenceWorkflowStatus:
        return self._status

    @workflow.query
    def drafts(self) -> ReferenceDraftResult | None:
        """Query-visible drafting outcome: counts and IDs, never a reference."""
        return self._drafts
