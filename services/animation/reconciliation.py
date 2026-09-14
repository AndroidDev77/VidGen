"""Releasing T15 items stranded on an ambiguous Runway submission.

An ambiguous submission is a submission whose outcome nobody knows: the
transport failed after the request could already have reached Runway, so a
remote task may exist, may be running, and may be billed. The pipeline parks
such an item in ``provider_outcome_ambiguous`` and refuses every later attempt,
because resubmitting blind is how a project pays twice for one shot.

That protection is correct and stays. What was missing is the way *out* of it:
nothing cleared the status, so an item that got there - including every item put
there by a local failure that was never ambiguous at all - stayed stuck forever,
holding a ``RESERVED`` cost reservation against the project's budget.

This module is that way out, and it only ever moves an item when no remote task
can exist:

* The attempt must own no ``remote_task_id`` and no generated video. Either one
  means the submission was not lost, and the item is refused rather than
  released - it has a task to poll.
* Then the provider's side has to be ruled out. Runway offers no way to look up
  a task whose ID we never received, so exactly two things can establish it: the
  durable attempt itself recording that the request never left the process, or a
  named operator confirming against the provider that no task exists. The
  attestation is persisted with the attempt it released, so the release is
  always attributable afterwards.

A reconciled item goes back to ``animation_failed`` with its task marked
``submission_failed``, which is the state the pipeline already knows how to
resubmit from, and its reservation is released.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from services.animation.pipeline import SUBMISSION_NOT_SENT_CODE
from vidgen.contracts.animation import (
    MAX_RECONCILED_ITEMS,
    AmbiguousSubmissionEvidence,
    AmbiguousSubmissionOutcome,
    AmbiguousSubmissionReconciliation,
    AmbiguousSubmissionReconciliationReport,
)
from vidgen.db.animation_models import (
    AnimationGeneratedVideo,
    AnimationItem,
    AnimationRun,
    RunwayTask,
)
from vidgen.db.cost_repository import CostRepository

_LOGGER = logging.getLogger("vidgen.animation.reconciliation")

#: The item status the pipeline parks an ambiguous submission in.
AMBIGUOUS_ITEM_STATUS = "provider_outcome_ambiguous"
#: The error code a reconciled item carries afterwards. It is deliberately not
#: a provider error: nothing about the provider was learned, a human released it.
RECONCILED_ERROR_CODE = "AMBIGUOUS_SUBMISSION_RECONCILED"
#: Bound on the attestation note persisted with a released attempt.
MAX_NOTE_LENGTH = 500

@dataclass(frozen=True, slots=True)
class OperatorAttestation:
    """A named operator's confirmation that the provider holds no such task.

    ``verified_no_remote_task`` is not a formality: it is the only statement
    that can stand in for a provider lookup Runway does not offer, and it is
    recorded against every attempt it releases.
    """

    subject: str
    verified_no_remote_task: bool = False
    note: str = ""


def ambiguous_animation_items(
    session: Session, project_id: UUID, *, shot_ids: tuple[UUID, ...] = ()
) -> list[AnimationItem]:
    """Every item of ``project_id`` parked on an ambiguous submission."""
    statement = (
        select(AnimationItem)
        .join(AnimationRun, AnimationRun.id == AnimationItem.run_id)
        .where(
            AnimationRun.project_id == project_id,
            AnimationItem.status == AMBIGUOUS_ITEM_STATUS,
        )
        .order_by(AnimationItem.shot_sequence, AnimationItem.created_at)
    )
    if shot_ids:
        statement = statement.where(AnimationItem.shot_id.in_(shot_ids))
    return list(session.scalars(statement).all())


def reconcile_ambiguous_submissions(
    session: Session,
    *,
    project_id: UUID,
    attestation: OperatorAttestation,
    shot_ids: tuple[UUID, ...] = (),
) -> AmbiguousSubmissionReconciliationReport:
    """Return every ambiguous item that can be proven task-free to a retry.

    The caller keeps the transaction: this flushes, never commits, so a request
    that fails afterwards leaves no half-released item behind.
    """
    items = ambiguous_animation_items(session, project_id, shot_ids=shot_ids)[:MAX_RECONCILED_ITEMS]
    costs = CostRepository(session)
    outcomes = [_reconcile_item(session, costs, item, attestation) for item in items]
    session.flush()
    reconciled = sum(
        outcome.outcome == AmbiguousSubmissionOutcome.RECONCILED for outcome in outcomes
    )
    return AmbiguousSubmissionReconciliationReport(
        project_id=project_id,
        examined_count=len(outcomes),
        reconciled_count=reconciled,
        refused_count=len(outcomes) - reconciled,
        items=outcomes,
    )


def _reconcile_item(
    session: Session,
    costs: CostRepository,
    item: AnimationItem,
    attestation: OperatorAttestation,
) -> AmbiguousSubmissionReconciliation:
    task = session.scalar(
        select(RunwayTask)
        .where(RunwayTask.animation_item_id == item.id)
        .order_by(RunwayTask.created_at.desc())
    )
    if task is None:
        # An ambiguous item always has the pre-call checkpoint that made it
        # ambiguous. Without one there is nothing to verify against, so the row
        # is left exactly as it is rather than guessed at.
        return AmbiguousSubmissionReconciliation(
            animation_item_id=item.id,
            shot_id=item.shot_id,
            outcome=AmbiguousSubmissionOutcome.ATTESTATION_REQUIRED,
            detail="the item has no submission checkpoint to reconcile against",
        )
    existing = _existing_remote_task(session, item, task)
    if existing is not None:
        return AmbiguousSubmissionReconciliation(
            animation_item_id=item.id,
            shot_id=item.shot_id,
            outcome=AmbiguousSubmissionOutcome.REMOTE_TASK_EXISTS,
            remote_task_id=existing,
            detail="the attempt owns a remote task; poll or cancel it instead of releasing it",
        )
    evidence = _evidence(task, attestation)
    if evidence is None:
        return AmbiguousSubmissionReconciliation(
            animation_item_id=item.id,
            shot_id=item.shot_id,
            outcome=AmbiguousSubmissionOutcome.ATTESTATION_REQUIRED,
            detail=(
                "nothing proves the provider created no task; confirm against the provider "
                "and resubmit with an explicit no-remote-task attestation"
            ),
        )
    released = _release(costs, task)
    item.status = "animation_failed"
    item.error_code = RECONCILED_ERROR_CODE
    task.provider_status = "submission_failed"
    task.failure_code = RECONCILED_ERROR_CODE
    task.response_metadata = {
        **dict(task.response_metadata or {}),
        "reconciled_at": datetime.now(UTC).isoformat(),
        "reconciled_by": attestation.subject[:255],
        "reconciliation_evidence": evidence.value,
        "reconciliation_note": attestation.note[:MAX_NOTE_LENGTH],
    }
    _LOGGER.info(
        "released an ambiguous T15 submission back to a retryable state",
        extra={
            "animationItemId": str(item.id),
            "shotId": str(item.shot_id),
            "evidence": evidence.value,
        },
    )
    return AmbiguousSubmissionReconciliation(
        animation_item_id=item.id,
        shot_id=item.shot_id,
        outcome=AmbiguousSubmissionOutcome.RECONCILED,
        evidence=evidence,
        released_reservation_id=released,
        detail="returned to a retryable state; the shot's next retry resubmits it",
    )


def _existing_remote_task(session: Session, item: AnimationItem, task: RunwayTask) -> str | None:
    """The remote task this attempt already owns, if the database knows of one."""
    if task.remote_task_id:
        return task.remote_task_id
    video = session.scalar(
        select(AnimationGeneratedVideo).where(AnimationGeneratedVideo.animation_item_id == item.id)
    )
    return video.remote_task_id if video is not None else None


def _evidence(
    task: RunwayTask, attestation: OperatorAttestation
) -> AmbiguousSubmissionEvidence | None:
    """How - if at all - this attempt can be shown to hold no remote task."""
    if task.failure_code == SUBMISSION_NOT_SENT_CODE:
        # Written by an adapter that proved the request never left the process.
        # A worker running an older classification could park such a failure as
        # ambiguous, and its own record is enough to release it again.
        return AmbiguousSubmissionEvidence.PROVIDER_NEVER_SENT
    if attestation.verified_no_remote_task and attestation.subject.strip():
        return AmbiguousSubmissionEvidence.OPERATOR_ATTESTATION
    return None


def _release(costs: CostRepository, task: RunwayTask) -> UUID | None:
    """Release the reservation the never-completed submit is still holding."""
    metadata = dict(task.response_metadata or {})
    reservation = metadata.get("reservation_id")
    if not reservation:
        return None
    base_key = str(metadata.get("attempt_key") or task.id)
    reservation_id = UUID(str(reservation))
    try:
        costs.reconcile(reservation_id, f"{base_key}:reconciliation", Decimal("0"), billable=False)
    except (ValueError, LookupError):
        # Already reconciled, or its budget is gone. Neither is a reason to
        # leave the item stranded - releasing money is the lesser half of this.
        _LOGGER.warning(
            "ambiguous submission reservation could not be released",
            extra={"reservationId": str(reservation_id)},
        )
        return None
    return reservation_id
