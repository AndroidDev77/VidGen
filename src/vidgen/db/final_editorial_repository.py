"""Persistence for T22 final editorial QA.

The repository is the only place that reads or writes the final-QA tables, so
the reuse rules live in one place: a completed run with the same final-QA
identity is returned as-is, a completed phase is never re-executed, an existing
provider attempt is reattached rather than duplicated, and a report is written
exactly once and never overwritten.

The completion gate is read from here too, so "may this project finish?" has a
single answer derived from persisted rows rather than from UI state.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from vidgen.contracts.final_editorial import (
    TERMINAL_STATUSES,
    FinalDeterministicCheck,
    FinalQADecision,
    FinalQAPhase,
    FinalQAStatus,
)
from vidgen.db.final_editorial_models import (
    FinalCompletionGate,
    FinalEditorialCheckRecord,
    FinalEditorialProviderAttempt,
    FinalEditorialReview,
    FinalEditorialRun,
)


class FinalEditorialConcurrencyError(RuntimeError):
    """A human decision lost an optimistic-concurrency race and was discarded."""


class FinalEditorialRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    # --- runs -------------------------------------------------------------
    def run_by_identity(self, final_qa_identity: str) -> FinalEditorialRun | None:
        return self._session.scalar(
            select(FinalEditorialRun).where(
                FinalEditorialRun.final_qa_identity == final_qa_identity
            )
        )

    def run_by_key(self, project_id: UUID, idempotency_key: str) -> FinalEditorialRun | None:
        return self._session.scalar(
            select(FinalEditorialRun).where(
                FinalEditorialRun.project_id == project_id,
                FinalEditorialRun.idempotency_key == idempotency_key,
            )
        )

    def run(self, project_id: UUID, final_editorial_run_id: UUID) -> FinalEditorialRun | None:
        return self._session.scalar(
            select(FinalEditorialRun).where(
                FinalEditorialRun.id == final_editorial_run_id,
                FinalEditorialRun.project_id == project_id,
            )
        )

    def runs_for_project(self, project_id: UUID) -> list[FinalEditorialRun]:
        return list(
            self._session.scalars(
                select(FinalEditorialRun)
                .where(FinalEditorialRun.project_id == project_id)
                .order_by(FinalEditorialRun.created_at.desc())
            )
        )

    def selected_run(self, project_id: UUID) -> FinalEditorialRun | None:
        """The current selected report for the project, if one exists."""
        return self._session.scalar(
            select(FinalEditorialRun)
            .where(
                FinalEditorialRun.project_id == project_id,
                FinalEditorialRun.selected.is_(True),
            )
            .order_by(FinalEditorialRun.created_at.desc())
        )

    def is_complete(self, run: FinalEditorialRun) -> bool:
        return run.status in {status.value for status in TERMINAL_STATUSES}

    def phase_complete(self, run: FinalEditorialRun, phase: FinalQAPhase) -> bool:
        """A completed phase is reused whenever its inputs are unchanged.

        The identity already binds every material input, so a phase recorded
        against this run was computed from exactly these inputs.
        """
        return phase.value in list(run.completed_phases or [])

    def checkpoint(
        self,
        run: FinalEditorialRun,
        *,
        status: FinalQAStatus,
        phase: FinalQAPhase,
        completed: FinalQAPhase | None = None,
        error_code: str | None = None,
    ) -> None:
        run.status = status.value
        run.current_phase = phase.value
        if completed is not None:
            phases = list(run.completed_phases or [])
            if completed.value not in phases:
                phases.append(completed.value)
            run.completed_phases = phases
        if error_code is not None:
            run.error_code = error_code
        if status in TERMINAL_STATUSES and run.completed_at is None:
            run.completed_at = datetime.now(UTC)
        self._session.flush()

    def select(self, run: FinalEditorialRun) -> None:
        """Promote this run's report as the current one for its render.

        A previously selected run for the same render loses the flag first, so
        the partial unique index never sees two selected rows at once.
        """
        self._session.execute(
            update(FinalEditorialRun)
            .where(
                FinalEditorialRun.final_render_asset_id == run.final_render_asset_id,
                FinalEditorialRun.id != run.id,
                FinalEditorialRun.selected.is_(True),
            )
            .values(selected=False)
        )
        self._session.flush()
        run.selected = True
        self._session.flush()

    # --- checks -----------------------------------------------------------
    def checks(self, final_editorial_run_id: UUID) -> list[FinalEditorialCheckRecord]:
        return list(
            self._session.scalars(
                select(FinalEditorialCheckRecord)
                .where(FinalEditorialCheckRecord.final_editorial_run_id == final_editorial_run_id)
                .order_by(FinalEditorialCheckRecord.created_at, FinalEditorialCheckRecord.id)
            )
        )

    def persist_checks(
        self, run: FinalEditorialRun, checks: list[FinalDeterministicCheck]
    ) -> list[FinalEditorialCheckRecord]:
        """Write each check exactly once; a resumed run reattaches to its rows."""
        existing = {record.check_key: record for record in self.checks(run.id)}
        now = datetime.now(UTC)
        rows: list[FinalEditorialCheckRecord] = []
        for check in checks:
            if check.check_id in existing:
                rows.append(existing[check.check_id])
                continue
            evidence = (
                [
                    {
                        "code": check.code.value,
                        "start_us": check.start_us,
                        "end_us": check.end_us,
                        "tool": check.tool,
                        "tool_version": check.tool_version,
                    }
                ]
                if check.status == "fail"
                else []
            )
            row = FinalEditorialCheckRecord(
                final_editorial_run_id=run.id,
                check_key=check.check_id,
                check_type=check.check_type.value,
                check_code=check.code.value,
                check_version=check.check_version,
                status=check.status,
                blocking=check.blocking,
                measurements=(
                    {"value": check.measurement, "unit": check.unit}
                    if check.measurement is not None
                    else {}
                ),
                thresholds=(
                    {"value": check.threshold, "unit": check.unit}
                    if check.threshold is not None
                    else {}
                ),
                findings=[{"message": check.message}] if check.status == "fail" else [],
                evidence_references=evidence,
                evidence_count=len(evidence),
                start_us=check.start_us,
                end_us=check.end_us,
                tool=check.tool,
                tool_version=check.tool_version,
                message=check.message[:500],
                created_at=now,
                completed_at=now,
            )
            self._session.add(row)
            rows.append(row)
        self._session.flush()
        return rows

    # --- provider attempts ------------------------------------------------
    def attempts(self, final_editorial_run_id: UUID) -> list[FinalEditorialProviderAttempt]:
        return list(
            self._session.scalars(
                select(FinalEditorialProviderAttempt)
                .where(
                    FinalEditorialProviderAttempt.final_editorial_run_id == final_editorial_run_id
                )
                .order_by(FinalEditorialProviderAttempt.created_at)
            )
        )

    def attempt_by_identity(self, attempt_identity: str) -> FinalEditorialProviderAttempt | None:
        return self._session.scalar(
            select(FinalEditorialProviderAttempt).where(
                FinalEditorialProviderAttempt.attempt_identity == attempt_identity
            )
        )

    def next_attempt_number(self, final_editorial_run_id: UUID, phase: FinalQAPhase) -> int:
        rows = [
            attempt
            for attempt in self.attempts(final_editorial_run_id)
            if attempt.phase == phase.value
        ]
        return len(rows) + 1

    # --- human review -----------------------------------------------------
    def reviews(self, final_editorial_run_id: UUID) -> list[FinalEditorialReview]:
        return list(
            self._session.scalars(
                select(FinalEditorialReview)
                .where(FinalEditorialReview.final_editorial_run_id == final_editorial_run_id)
                .order_by(FinalEditorialReview.created_at)
            )
        )

    def resolved_finding_ids(self, final_editorial_run_id: UUID) -> frozenset[UUID]:
        """Findings a reviewer has accepted; a rejection never clears the gate."""
        return frozenset(
            review.finding_id
            for review in self.reviews(final_editorial_run_id)
            if review.decision == "accept"
        )

    def record_review(
        self,
        run: FinalEditorialRun,
        *,
        finding_id: UUID,
        reviewer_subject: str,
        decision: str,
        reason_code: str,
        reason: str,
        expected_row_version: int,
        idempotency_key: str | None = None,
    ) -> FinalEditorialReview:
        """Record one adjudication, rejecting a stale or duplicate decision."""
        existing = self._session.scalar(
            select(FinalEditorialReview).where(
                FinalEditorialReview.final_editorial_run_id == run.id,
                FinalEditorialReview.finding_id == finding_id,
            )
        )
        if existing is not None:
            if (
                idempotency_key is not None
                and existing.idempotency_key == idempotency_key
                and existing.decision == decision
            ):
                return existing
            raise FinalEditorialConcurrencyError("this finding has already been adjudicated")
        review = FinalEditorialReview(
            final_editorial_run_id=run.id,
            finding_id=finding_id,
            reviewer_subject=reviewer_subject[:255],
            decision=decision,
            reason_code=reason_code[:64],
            reason=reason[:1000],
            expected_row_version=expected_row_version,
            idempotency_key=idempotency_key,
            created_at=datetime.now(UTC),
        )
        self._session.add(review)
        self._session.flush()
        return review

    # --- completion gate --------------------------------------------------
    def gates(self, project_id: UUID) -> list[FinalCompletionGate]:
        return list(
            self._session.scalars(
                select(FinalCompletionGate)
                .where(FinalCompletionGate.project_id == project_id)
                .order_by(FinalCompletionGate.created_at.desc())
            )
        )

    def record_gate(
        self,
        run: FinalEditorialRun,
        *,
        decision: FinalQADecision,
        blocking_finding_count: int,
        review_finding_count: int,
        deterministic_failure_count: int,
        gate_version: str,
        reasons: list[str],
    ) -> FinalCompletionGate:
        """Write the immutable gate row, or return the one already written."""
        existing = self._session.scalar(
            select(FinalCompletionGate).where(
                FinalCompletionGate.final_editorial_run_id == run.id,
                FinalCompletionGate.gate_version == gate_version,
            )
        )
        if existing is not None:
            return existing
        gate = FinalCompletionGate(
            project_id=run.project_id,
            final_editorial_run_id=run.id,
            final_render_asset_id=run.final_render_asset_id,
            render_identity=run.render_identity,
            decision=decision.value,
            blocking_finding_count=blocking_finding_count,
            review_finding_count=review_finding_count,
            deterministic_failure_count=deterministic_failure_count,
            gate_version=gate_version,
            reasons=reasons[:32],
            created_at=datetime.now(UTC),
        )
        self._session.add(gate)
        self._session.flush()
        return gate

    def completion_gate(
        self, project_id: UUID, final_render_asset_id: UUID | None
    ) -> tuple[bool, str]:
        """Whether the project may reach its final completed state, and why not.

        A gate for an older render never clears the current one: the decision
        must belong to the render the project actually holds.
        """
        if final_render_asset_id is None:
            return False, "final_render_missing"
        run = self._session.scalar(
            select(FinalEditorialRun)
            .where(
                FinalEditorialRun.project_id == project_id,
                FinalEditorialRun.final_render_asset_id == final_render_asset_id,
                FinalEditorialRun.selected.is_(True),
            )
            .order_by(FinalEditorialRun.created_at.desc())
        )
        if run is None:
            return False, "final_qa_missing"
        if run.final_decision == FinalQADecision.PASS.value:
            return True, "final_qa_pass"
        if run.final_decision == FinalQADecision.REVIEW.value:
            return False, "final_qa_review_required"
        return False, "final_qa_failed"
