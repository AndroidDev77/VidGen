from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from vidgen.contracts.costs import (
    BudgetDecision,
    CostReconciliationResult,
    CostReservationRequest,
    CostReservationResult,
)
from vidgen.db.cost_models import CostLedgerEntry, CostReservation, ProjectBudget, ProviderAttempt


class BudgetExceededError(RuntimeError):
    pass


class CostRepository:
    def __init__(self, session: Session):
        self.session = session

    def reserve(self, request: CostReservationRequest) -> CostReservationResult:
        existing = self.session.scalar(
            select(CostReservation).where(
                CostReservation.idempotency_key == request.idempotency_key
            )
        )
        if existing:
            return CostReservationResult(
                reservation_id=existing.id,
                decision=BudgetDecision.ALLOW,
                reserved_amount=existing.reserved_amount,
                reused=True,
            )
        budget = self.session.scalar(
            select(ProjectBudget)
            .where(ProjectBudget.project_id == request.project_id)
            .with_for_update()
        )
        if budget is None:
            raise LookupError("project budget not configured")
        if budget.currency != request.currency:
            raise ValueError("budget currency mismatch")
        if (
            budget.committed_amount + budget.reserved_amount + request.estimated_amount
            > budget.hard_cap
        ):
            return CostReservationResult(
                reservation_id=None,
                decision=BudgetDecision.DENY_HARD_CAP,
                reserved_amount=Decimal("0"),
            )
        reservation = CostReservation(
            project_id=request.project_id,
            provider_attempt_id=request.provider_attempt_id,
            idempotency_key=request.idempotency_key,
            estimated_amount=request.estimated_amount,
            reserved_amount=request.estimated_amount,
            status="RESERVED",
        )
        budget.reserved_amount += request.estimated_amount
        budget.row_version += 1
        self.session.add(reservation)
        self.session.flush()
        decision = (
            BudgetDecision.ALLOW_WITH_WARNING
            if budget.committed_amount + budget.reserved_amount >= budget.warning_cap
            else BudgetDecision.ALLOW
        )
        return CostReservationResult(
            reservation_id=reservation.id,
            decision=decision,
            reserved_amount=reservation.reserved_amount,
        )

    def reconcile(
        self, reservation_id: UUID, idempotency_key: str, actual: Decimal, *, billable: bool = True
    ) -> CostReconciliationResult:
        existing = self.session.scalar(
            select(CostLedgerEntry).where(CostLedgerEntry.idempotency_key == idempotency_key)
        )
        if existing:
            return CostReconciliationResult(
                ledger_entry_id=existing.id,
                committed_amount=existing.actual_amount,
                released_amount=existing.released_amount,
                reused=True,
            )
        reservation = self.session.scalar(
            select(CostReservation).where(CostReservation.id == reservation_id).with_for_update()
        )
        if reservation is None or reservation.status != "RESERVED":
            raise ValueError("reservation cannot be reconciled")
        attempt = self.session.get(ProviderAttempt, reservation.provider_attempt_id)
        budget = self.session.scalar(
            select(ProjectBudget)
            .where(ProjectBudget.project_id == reservation.project_id)
            .with_for_update()
        )
        if attempt is None or budget is None:
            raise LookupError("reservation dependencies missing")
        committed = actual if billable else Decimal("0")
        released = max(Decimal("0"), reservation.reserved_amount - committed)
        budget.reserved_amount -= reservation.reserved_amount
        budget.committed_amount += committed
        budget.released_amount += released
        now = datetime.now(UTC)
        reservation.status = "COMMITTED" if committed else "RELEASED"
        reservation.reconciled_at = now
        reservation.released_at = now if released else None
        entry = CostLedgerEntry(
            project_id=reservation.project_id,
            provider_attempt_id=attempt.id,
            reservation_id=reservation.id,
            provider=attempt.provider,
            model=attempt.model,
            operation=attempt.operation,
            reason="provider_attempt",
            pricing_version_id=attempt.pricing_version_id,
            currency=attempt.currency,
            estimated_amount=reservation.estimated_amount,
            reserved_amount=reservation.reserved_amount,
            actual_amount=committed,
            released_amount=released,
            usage=attempt.usage,
            status="COMMITTED" if committed else "RELEASED",
            idempotency_key=idempotency_key,
            trace_id=attempt.trace_id,
            reserved_at=reservation.created_at,
            reconciled_at=now,
            released_at=now if released else None,
        )
        self.session.add(entry)
        self.session.flush()
        return CostReconciliationResult(
            ledger_entry_id=entry.id, committed_amount=committed, released_amount=released
        )

    def totals(self, project_id: UUID) -> tuple[Decimal, Decimal, Decimal]:
        return tuple(
            Decimal(v or 0)
            for v in self.session.execute(
                select(
                    func.sum(CostLedgerEntry.reserved_amount),
                    func.sum(CostLedgerEntry.actual_amount),
                    func.sum(CostLedgerEntry.released_amount),
                ).where(CostLedgerEntry.project_id == project_id)
            ).one()
        )  # type: ignore[return-value]
