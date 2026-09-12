"""Reconciling a project's recorded workflow run with the cluster's own view.

``project_workflow_runs`` is written by whoever started the execution, and
nothing writes to it again when that execution dies: a workflow that fails
validation inside an activity leaves the row saying ``running`` forever. The
project's generation run is left ``active`` for the same reason, and the partial
unique index that allows exactly one active run per project then refuses every
continuation with ``project_generation_run_active``.

The result was a project that could only be restarted by editing two rows by
hand. This module closes that gap from the read side: whenever the API is about
to answer a question about the workflow - or to start a new run - it asks the
cluster what actually happened and settles the stale rows first.

Nothing here starts, signals or cancels a workflow. An execution the cluster
reports as terminal is already over; this only writes down that it is.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from services.control_plane.generation_runs import GenerationRunService
from vidgen.contracts.control_commands import ProjectGenerationRunStatus
from vidgen.db.control_command_models import ProjectGenerationRunRecord
from vidgen.db.workflow_models import ProjectWorkflowRun
from vidgen.review.events import ProjectEventService
from vidgen.review.workflow_control import (
    EXECUTION_CANCELLED,
    EXECUTION_COMPLETED,
    EXECUTION_FAILED,
    EXECUTION_TERMINATED,
    EXECUTION_TIMED_OUT,
    LIVE_RUN_STATUSES,
    TERMINAL_EXECUTION_STATUSES,
    WorkflowController,
)

_LOGGER = logging.getLogger("vidgen.control_plane.reconciliation")

#: Re-exported for the callers that already import it from here: a run status
#: claiming the execution is live is left alone by nobody but this module.
__all__ = ["LIVE_RUN_STATUSES", "Reconciliation", "reconcile_project_workflow"]

#: What each terminal execution status means for the recorded run and for the
#: generation run it opened.
_SETTLEMENT: dict[str, tuple[str, ProjectGenerationRunStatus]] = {
    EXECUTION_COMPLETED: ("completed", ProjectGenerationRunStatus.COMPLETED),
    EXECUTION_FAILED: ("failed", ProjectGenerationRunStatus.FAILED),
    EXECUTION_CANCELLED: ("cancelled", ProjectGenerationRunStatus.CANCELLED),
    EXECUTION_TERMINATED: ("cancelled", ProjectGenerationRunStatus.CANCELLED),
    EXECUTION_TIMED_OUT: ("failed", ProjectGenerationRunStatus.FAILED),
}


@dataclass(frozen=True, slots=True)
class Reconciliation:
    """What one reconciliation pass found and settled."""

    #: The execution status the cluster reported, when it could be asked.
    execution_status: str | None = None
    #: The status the recorded workflow run was moved to, if it moved.
    run_status: str | None = None
    #: Whether a stale *active* generation run was settled by this pass.
    settled_generation_run: bool = False

    @property
    def changed(self) -> bool:
        return self.run_status is not None or self.settled_generation_run


def reconcile_project_workflow(
    session: Session,
    *,
    project_id: UUID,
    controller: WorkflowController,
    run: ProjectWorkflowRun | None = None,
) -> Reconciliation:
    """Settle the rows of an execution the cluster says is already over.

    The caller keeps the transaction: this flushes, never commits, so a request
    that fails afterwards does not leave a half-reconciled project behind.
    """
    if run is None:
        run = session.scalar(
            select(ProjectWorkflowRun).where(ProjectWorkflowRun.project_id == project_id)
        )
    if run is None or run.status not in LIVE_RUN_STATUSES:
        return Reconciliation()

    try:
        status = controller.project_execution_status(run.workflow_id)
    except Exception:  # pragma: no cover - defensive: never fail a read on this
        # An unreachable cluster is not evidence that anything stopped. The next
        # request reconciles instead.
        _LOGGER.warning(
            "could not read the execution status for a project workflow",
            extra={"projectId": str(project_id), "workflowId": run.workflow_id},
        )
        return Reconciliation()
    if status is None or status not in TERMINAL_EXECUTION_STATUSES:
        return Reconciliation(execution_status=status)

    run_status, generation_status = _SETTLEMENT[status]
    run.status = run_status
    settled = _settle_generation_run(session, run=run, status=generation_status)
    session.flush()
    # The event payload is a bounded allow-list, so the execution status travels
    # as the event's own ``status``: "failed" here means the cluster said so.
    ProjectEventService(session).append(
        project_id,
        event_type="workflow_reconciled",
        status=run_status,
        workflow_id=run.workflow_id,
    )
    _LOGGER.info(
        "reconciled a stale project workflow run",
        extra={
            "projectId": str(project_id),
            "workflowId": run.workflow_id,
            "executionStatus": status,
        },
    )
    return Reconciliation(
        execution_status=status, run_status=run_status, settled_generation_run=settled
    )


def _settle_generation_run(
    session: Session, *, run: ProjectWorkflowRun, status: ProjectGenerationRunStatus
) -> bool:
    """Close out the active generation run, but only if it is *this* execution's.

    A project's stable workflow ID is reused by every continuation, so the
    active run may already belong to a newer execution than the row being
    settled. Matching on the recorded run ID keeps this from cancelling work
    that has only just started.
    """
    runs = GenerationRunService(session)
    active = runs.active(run.project_id)
    if active is None or not _belongs_to(active, run):
        return False
    runs.settle(active, status)
    return True


def _belongs_to(active: ProjectGenerationRunRecord, run: ProjectWorkflowRun) -> bool:
    if active.workflow_id is not None and active.workflow_id != run.workflow_id:
        return False
    # A run that never recorded an execution identity - the one ``workflow:start``
    # opens before the workflow answers - belongs to this project's only
    # execution by construction.
    if active.run_id is None or run.run_id is None:
        return True
    return active.run_id == run.run_id
