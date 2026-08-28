"""Persistence for T20 visual QA.

The repository is the only place that reads or writes the QA tables, so the
reuse rules live in one place: a completed run with the same QA identity is
returned as-is, its samples, attempts, results and evidence are never
duplicated, and a resumed run reattaches to the rows it already created rather
than creating new ones.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from vidgen.contracts.visual_qa import (
    VisualQAAttemptType,
    VisualQAEvidence,
    VisualQAOutcome,
    VisualQASample,
    VisualQATargetType,
)
from vidgen.db.visual_qa_models import (
    VisualQAAttempt,
    VisualQAEvidenceRecord,
    VisualQAHumanReview,
    VisualQAResultRecord,
    VisualQARun,
    VisualQASampleRecord,
)

TERMINAL_STATUSES = frozenset({"visual_qa_complete", "visual_qa_failed"})


class VisualQARepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    # --- runs -------------------------------------------------------------
    def run_by_identity(self, qa_identity: str) -> VisualQARun | None:
        return self._session.scalar(
            select(VisualQARun).where(VisualQARun.qa_identity == qa_identity)
        )

    def run_by_key(
        self, project_id: UUID, idempotency_key: str, target_type: VisualQATargetType
    ) -> VisualQARun | None:
        return self._session.scalar(
            select(VisualQARun).where(
                VisualQARun.project_id == project_id,
                VisualQARun.idempotency_key == idempotency_key,
                VisualQARun.target_type == target_type.value,
            )
        )

    def runs_for_project(self, project_id: UUID) -> list[VisualQARun]:
        return list(
            self._session.scalars(
                select(VisualQARun)
                .where(VisualQARun.project_id == project_id)
                .order_by(VisualQARun.created_at)
            )
        )

    def runs_for_shot(self, project_id: UUID, shot_id: UUID) -> list[VisualQARun]:
        return list(
            self._session.scalars(
                select(VisualQARun)
                .where(VisualQARun.project_id == project_id, VisualQARun.shot_id == shot_id)
                .order_by(VisualQARun.created_at)
            )
        )

    def runs_for_shot_any_project(self, shot_id: UUID) -> list[VisualQARun]:
        """Every QA run for one shot, used to prove a sibling was left untouched."""
        return list(
            self._session.scalars(
                select(VisualQARun)
                .where(VisualQARun.shot_id == shot_id)
                .order_by(VisualQARun.created_at)
            )
        )

    def run(self, project_id: UUID, qa_run_id: UUID) -> VisualQARun | None:
        return self._session.scalar(
            select(VisualQARun).where(
                VisualQARun.id == qa_run_id, VisualQARun.project_id == project_id
            )
        )

    def canonical_run(self, shot_id: UUID, target_type: VisualQATargetType) -> VisualQARun | None:
        """The most recent completed QA run for one shot and target type."""
        return self._session.scalar(
            select(VisualQARun)
            .where(
                VisualQARun.shot_id == shot_id,
                VisualQARun.target_type == target_type.value,
                VisualQARun.status == "visual_qa_complete",
            )
            .order_by(VisualQARun.created_at.desc())
        )

    def is_complete(self, run: VisualQARun) -> bool:
        return run.status == "visual_qa_complete" and run.selected_result_id is not None

    # --- samples ----------------------------------------------------------
    def samples(self, qa_run_id: UUID) -> list[VisualQASampleRecord]:
        return list(
            self._session.scalars(
                select(VisualQASampleRecord)
                .where(VisualQASampleRecord.qa_run_id == qa_run_id)
                .order_by(VisualQASampleRecord.sequence)
            )
        )

    def persist_samples(
        self, qa_run_id: UUID, samples: Sequence[VisualQASample]
    ) -> list[VisualQASampleRecord]:
        """Persist a sample plan once; a resumed run reuses the stored rows."""
        existing = self.samples(qa_run_id)
        if existing:
            return existing
        now = datetime.now(UTC)
        rows = [
            VisualQASampleRecord(
                id=sample.sample_id,
                qa_run_id=qa_run_id,
                sequence=sample.sequence,
                sample_type=sample.sample_type.value,
                requested_timestamp_us=sample.requested_timestamp_us,
                actual_timestamp_us=sample.actual_timestamp_us,
                shot_relative_timestamp_us=sample.shot_relative_timestamp_us,
                frame_asset_id=sample.frame_asset_id,
                frame_sha256=sample.frame_sha256,
                source_asset_id=sample.source_asset_id,
                selection_reason=sample.selection_reason[:255],
                contact_sheet_position=sample.contact_sheet_position,
                measurements=dict(sample.measurements),
                created_at=now,
            )
            for sample in samples
        ]
        self._session.add_all(rows)
        self._session.flush()
        return rows

    # --- attempts ---------------------------------------------------------
    def attempts(self, qa_run_id: UUID) -> list[VisualQAAttempt]:
        return list(
            self._session.scalars(
                select(VisualQAAttempt)
                .where(VisualQAAttempt.qa_run_id == qa_run_id)
                .order_by(VisualQAAttempt.attempt_number)
            )
        )

    def attempt_by_identity(self, attempt_identity: str) -> VisualQAAttempt | None:
        return self._session.scalar(
            select(VisualQAAttempt).where(VisualQAAttempt.attempt_identity == attempt_identity)
        )

    def succeeded_attempt(
        self, qa_run_id: UUID, attempt_type: VisualQAAttemptType
    ) -> VisualQAAttempt | None:
        """A completed provider evaluation whose result may be reused verbatim."""
        return self._session.scalar(
            select(VisualQAAttempt)
            .where(
                VisualQAAttempt.qa_run_id == qa_run_id,
                VisualQAAttempt.attempt_type == attempt_type.value,
                VisualQAAttempt.status == "succeeded",
            )
            .order_by(VisualQAAttempt.attempt_number.desc())
        )

    def next_attempt_number(self, qa_run_id: UUID, attempt_type: VisualQAAttemptType) -> int:
        numbers = [
            attempt.attempt_number
            for attempt in self.attempts(qa_run_id)
            if attempt.attempt_type == attempt_type.value
        ]
        return max(numbers, default=0) + 1

    # --- results and evidence --------------------------------------------
    def results(self, qa_run_id: UUID) -> list[VisualQAResultRecord]:
        return list(
            self._session.scalars(
                select(VisualQAResultRecord)
                .where(VisualQAResultRecord.qa_run_id == qa_run_id)
                .order_by(VisualQAResultRecord.created_at)
            )
        )

    def canonical_result(self, qa_run_id: UUID) -> VisualQAResultRecord | None:
        return self._session.scalar(
            select(VisualQAResultRecord).where(
                VisualQAResultRecord.qa_run_id == qa_run_id,
                VisualQAResultRecord.canonical.is_(True),
            )
        )

    def result_for_attempt(self, attempt_id: UUID) -> VisualQAResultRecord | None:
        return self._session.scalar(
            select(VisualQAResultRecord).where(VisualQAResultRecord.attempt_id == attempt_id)
        )

    def evidence(self, qa_result_id: UUID) -> list[VisualQAEvidenceRecord]:
        return list(
            self._session.scalars(
                select(VisualQAEvidenceRecord)
                .where(VisualQAEvidenceRecord.qa_result_id == qa_result_id)
                .order_by(VisualQAEvidenceRecord.created_at)
            )
        )

    def persist_evidence(
        self,
        qa_run_id: UUID,
        qa_result_id: UUID,
        findings: Sequence[tuple[UUID, VisualQAEvidence]],
    ) -> list[VisualQAEvidenceRecord]:
        """Persist a result's evidence once, anchored to this run's own samples."""
        existing = self.evidence(qa_result_id)
        if existing:
            return existing
        now = datetime.now(UTC)
        known = {sample.id for sample in self.samples(qa_run_id)}
        rows = [
            VisualQAEvidenceRecord(
                id=item.evidence_id,
                qa_result_id=qa_result_id,
                finding_id=finding_id,
                sample_id=item.sample_id if item.sample_id in known else None,
                frame_asset_id=item.frame_asset_id,
                shot_relative_timestamp_us=item.shot_relative_timestamp_us,
                source_relative_timestamp_us=item.source_relative_timestamp_us,
                bounding_box=item.bounding_box.model_dump(mode="json")
                if item.bounding_box
                else None,
                compared_reference_asset_id=item.compared_reference_asset_id,
                evidence_type=item.evidence_type.value,
                confidence=item.confidence,
                explanation=item.explanation[:500],
                created_at=now,
            )
            for finding_id, item in findings
        ]
        self._session.add_all(rows)
        self._session.flush()
        return rows

    def mark_canonical(self, run: VisualQARun, result: VisualQAResultRecord) -> None:
        """Promote exactly one result; the database enforces the uniqueness."""
        self._session.execute(
            update(VisualQAResultRecord)
            .where(
                VisualQAResultRecord.qa_run_id == run.id,
                VisualQAResultRecord.id != result.id,
                VisualQAResultRecord.canonical.is_(True),
            )
            .values(canonical=False)
        )
        self._session.flush()
        result.canonical = True
        run.selected_result_id = result.id
        self._session.flush()

    # --- human review -----------------------------------------------------
    def human_reviews(self, qa_run_id: UUID) -> list[VisualQAHumanReview]:
        return list(
            self._session.scalars(
                select(VisualQAHumanReview)
                .where(VisualQAHumanReview.qa_run_id == qa_run_id)
                .order_by(VisualQAHumanReview.created_at)
            )
        )

    def latest_human_review(self, qa_run_id: UUID) -> VisualQAHumanReview | None:
        reviews = self.human_reviews(qa_run_id)
        return reviews[-1] if reviews else None

    # --- gating -----------------------------------------------------------
    def gate(self, shot_id: UUID, target_type: VisualQATargetType) -> tuple[bool, str]:
        """Whether one shot may proceed past a T16 QA stage, and why not."""
        run = self.canonical_run(shot_id, target_type)
        if run is None:
            return False, "visual_qa_missing"
        if run.final_outcome == VisualQAOutcome.PASS.value:
            return True, "visual_qa_pass"
        if run.final_outcome == VisualQAOutcome.REVIEW.value:
            review = self.latest_human_review(run.id)
            if review is not None and review.decision == "approved":
                return True, "visual_qa_human_approved"
            return False, "visual_qa_review_required"
        return False, "visual_qa_failed"

    def project_gate(self, project_id: UUID, shot_ids: Sequence[UUID]) -> dict[UUID, str]:
        """Render-eligibility reasons for every shot of a render lineage."""
        del project_id
        return {shot_id: self.gate(shot_id, VisualQATargetType.VIDEO)[1] for shot_id in shot_ids}
