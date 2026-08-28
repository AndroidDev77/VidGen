"""Persistence for T21 repair and fallback routing.

The repository is the only place that reads or writes the repair tables, so the
bounded-policy rules live in one place: an identical request reuses its run, an
attempt identity is never created twice, ordinals are handed out densely, and a
repair run advances only for the worker that wins the conditional update.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, select, update
from sqlalchemy.orm import Session

from vidgen.contracts.repair import (
    HumanReviewReason,
    RepairAttemptKind,
    RepairAttemptStatus,
    RepairRoute,
    RepairRunState,
)
from vidgen.db.repair_models import (
    RepairAttemptRecord,
    RepairDecisionRecord,
    RepairFallbackRender,
    RepairRun,
    VeoOperationRecord,
)

TERMINAL_STATES = frozenset(
    {
        RepairRunState.LOCKED.value,
        RepairRunState.HUMAN_REVIEW_REQUIRED.value,
        RepairRunState.REPAIR_FAILED.value,
    }
)


class RepairConcurrencyError(RuntimeError):
    """Another worker already advanced this repair run past the read state."""


class RepairRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    # --- runs -------------------------------------------------------------
    def run_by_key(self, project_id: UUID, idempotency_key: str) -> RepairRun | None:
        return self._session.scalar(
            select(RepairRun).where(
                RepairRun.project_id == project_id,
                RepairRun.idempotency_key == idempotency_key,
            )
        )

    def run(self, project_id: UUID, repair_run_id: UUID) -> RepairRun | None:
        return self._session.scalar(
            select(RepairRun).where(
                RepairRun.id == repair_run_id, RepairRun.project_id == project_id
            )
        )

    def runs_for_shot(self, project_id: UUID, shot_id: UUID) -> list[RepairRun]:
        return list(
            self._session.scalars(
                select(RepairRun)
                .where(RepairRun.project_id == project_id, RepairRun.shot_id == shot_id)
                .order_by(RepairRun.created_at)
            )
        )

    def runs_for_project(self, project_id: UUID) -> list[RepairRun]:
        return list(
            self._session.scalars(
                select(RepairRun)
                .where(RepairRun.project_id == project_id)
                .order_by(RepairRun.created_at)
            )
        )

    def is_terminal(self, run: RepairRun) -> bool:
        return run.state in TERMINAL_STATES

    def claim_advance(self, run: RepairRun, *, expected_token: int) -> None:
        """Take the right to advance this run exactly once.

        Two workers that read the same ``advance_token`` both attempt this
        update; the database serialises them and the loser is told to re-read
        rather than driving the same route twice.
        """
        updated = cast(
            "CursorResult[Any]",
            self._session.execute(
                update(RepairRun)
                .where(RepairRun.id == run.id, RepairRun.advance_token == expected_token)
                .values(advance_token=expected_token + 1, updated_at=datetime.now(UTC))
            ),
        )
        if updated.rowcount != 1:
            raise RepairConcurrencyError(
                "another worker advanced this repair run; re-read before deciding again"
            )
        self._session.flush()
        self._session.refresh(run)

    def mark_state(
        self,
        run: RepairRun,
        state: RepairRunState,
        *,
        human_review_reason: HumanReviewReason | None = None,
        error_code: str | None = None,
    ) -> None:
        run.state = state.value
        if human_review_reason is not None:
            run.human_review_reason = human_review_reason.value
        elif state not in {RepairRunState.HUMAN_REVIEW_REQUIRED, RepairRunState.LOCKED}:
            # Leaving review clears the reason so a stale one cannot be shown
            # next to a run that is working again.
            run.human_review_reason = None
        if error_code is not None:
            run.error_code = error_code[:128]
        run.updated_at = datetime.now(UTC)

    # --- attempts ---------------------------------------------------------
    def attempts(self, repair_run_id: UUID) -> list[RepairAttemptRecord]:
        return list(
            self._session.scalars(
                select(RepairAttemptRecord)
                .where(RepairAttemptRecord.repair_run_id == repair_run_id)
                .order_by(RepairAttemptRecord.attempt_ordinal)
            )
        )

    def attempt_by_identity(self, attempt_identity: str) -> RepairAttemptRecord | None:
        return self._session.scalar(
            select(RepairAttemptRecord).where(
                RepairAttemptRecord.attempt_identity == attempt_identity
            )
        )

    def attempt(self, attempt_id: UUID) -> RepairAttemptRecord | None:
        return self._session.get(RepairAttemptRecord, attempt_id)

    def latest_attempt(self, repair_run_id: UUID) -> RepairAttemptRecord | None:
        rows = self.attempts(repair_run_id)
        return rows[-1] if rows else None

    def next_ordinal(self, repair_run_id: UUID) -> int:
        rows = self.attempts(repair_run_id)
        return (rows[-1].attempt_ordinal + 1) if rows else 0

    def counts(self, repair_run_id: UUID) -> dict[RepairAttemptKind, int]:
        """How many attempts of each kind this run has already spent."""
        tally = {kind: 0 for kind in RepairAttemptKind}
        for row in self.attempts(repair_run_id):
            tally[RepairAttemptKind(row.attempt_kind)] += 1
        return tally

    def selected_attempt(self, shot_id: UUID) -> RepairAttemptRecord | None:
        return self._session.scalar(
            select(RepairAttemptRecord).where(
                RepairAttemptRecord.shot_id == shot_id, RepairAttemptRecord.selected.is_(True)
            )
        )

    def select_attempt(self, attempt: RepairAttemptRecord, *, qa_result_id: UUID) -> None:
        """Make one revalidated attempt the authoritative output for its shot.

        Never called without a fresh passing T20 result: the database rejects a
        selection whose ``output_qa_result_id`` is missing, and it rejects a
        second selected attempt for the same shot.
        """
        if attempt.output_qa_result_id != qa_result_id:
            raise ValueError("an attempt is only selected on the QA result of its own output")
        self._session.execute(
            update(RepairAttemptRecord)
            .where(
                RepairAttemptRecord.shot_id == attempt.shot_id,
                RepairAttemptRecord.id != attempt.id,
                RepairAttemptRecord.selected.is_(True),
            )
            .values(selected=False)
        )
        attempt.status = RepairAttemptStatus.PASSED.value
        attempt.selected = True
        self._session.flush()

    # --- decisions --------------------------------------------------------
    def decisions(self, repair_run_id: UUID) -> list[RepairDecisionRecord]:
        return list(
            self._session.scalars(
                select(RepairDecisionRecord)
                .where(RepairDecisionRecord.repair_run_id == repair_run_id)
                .order_by(RepairDecisionRecord.sequence)
            )
        )

    def record_decision(
        self,
        run: RepairRun,
        *,
        route: RepairRoute,
        rationale: Sequence[str],
        planner_version: str,
        source_attempt_id: UUID | None = None,
        source_qa_result_id: UUID | None = None,
        classification: dict[str, Any] | None = None,
        failure_category: str | None = None,
        repair_codes: Sequence[str] = (),
        capability_profile_hash: str | None = None,
        budget_remaining: Decimal | None = None,
        estimated_next_cost: Decimal = Decimal("0"),
        human_review_reason: HumanReviewReason | None = None,
    ) -> RepairDecisionRecord:
        existing = self.decisions(run.id)
        record = RepairDecisionRecord(
            repair_run_id=run.id,
            sequence=len(existing),
            source_attempt_id=source_attempt_id,
            source_qa_result_id=source_qa_result_id,
            classification=classification,
            failure_category=failure_category,
            repair_codes=list(repair_codes),
            route=route.value,
            rationale=list(rationale),
            capability_profile_hash=capability_profile_hash,
            budget_remaining=budget_remaining,
            estimated_next_cost=estimated_next_cost,
            human_review_reason=(
                human_review_reason.value if human_review_reason is not None else None
            ),
            planner_version=planner_version,
            policy_version=run.policy_version,
            created_at=datetime.now(UTC),
        )
        self._session.add(record)
        self._session.flush()
        return record

    # --- fallback renders -------------------------------------------------
    def fallback_render(self, repair_attempt_id: UUID) -> RepairFallbackRender | None:
        return self._session.scalar(
            select(RepairFallbackRender).where(
                RepairFallbackRender.repair_attempt_id == repair_attempt_id
            )
        )

    # --- Veo operations ---------------------------------------------------
    def veo_operation(self, repair_attempt_id: UUID) -> VeoOperationRecord | None:
        return self._session.scalar(
            select(VeoOperationRecord).where(
                VeoOperationRecord.repair_attempt_id == repair_attempt_id
            )
        )

    def veo_operation_by_key(self, application_idempotency_key: str) -> VeoOperationRecord | None:
        return self._session.scalar(
            select(VeoOperationRecord).where(
                VeoOperationRecord.application_idempotency_key == application_idempotency_key
            )
        )
