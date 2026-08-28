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
        self._seen_approval_keys: set[str] = set()
        self._cancelled = False

    @workflow.run
    async def run(self, request: ReferenceWorkflowInput) -> ReferenceWorkflowResult:
        self._request = request
        self._status = ReferenceWorkflowStatus.SELECTING
        await workflow.execute_activity(
            "build_continuity_references",
            request,
            result_type=ReferenceDraftResult,
            start_to_close_timeout=timedelta(hours=2),
            heartbeat_timeout=timedelta(minutes=2),
            retry_policy=default_activity_retry_policy(),
        )
        if self._cancelled:
            return self._cancelled_result(request)
        self._status = ReferenceWorkflowStatus.AWAITING_APPROVAL
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
