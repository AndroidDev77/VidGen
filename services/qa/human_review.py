"""Owner-scoped human resolution of ambiguous T20 ``REVIEW`` outcomes.

A human can settle a semantic ambiguity the system could not resolve - "is this
really Maya?" - and nothing else. A reviewer can never clear a deterministic
corruption, a decode failure, or any hard failure: those are measured facts, not
judgement calls, and the automated result is preserved either way.

The decision never touches the generated asset. It records who decided, what
they decided, a bounded reason, and when.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from vidgen.contracts.review import ApiErrorCode
from vidgen.contracts.visual_qa import VisualQAOutcome, VisualQATargetType
from vidgen.db.visual_qa_models import VisualQAHumanReview, VisualQARun
from vidgen.db.visual_qa_repository import VisualQARepository
from vidgen.review.errors import ReviewError, conflict, not_found

MAX_REASON_LENGTH = 500


@dataclass(frozen=True, slots=True)
class HumanReviewOutcome:
    review_id: UUID
    qa_run_id: UUID
    decision: str
    row_version: int
    resulting_gate: str


class VisualQAHumanReviewService:
    def __init__(self, session: Session, reviewer_principal: str) -> None:
        self._session = session
        self._reviewer = reviewer_principal
        self._repository = VisualQARepository(session)

    def decide(
        self,
        run: VisualQARun,
        *,
        decision: str,
        reason: str,
        row_version: int,
        idempotency_key: str,
    ) -> HumanReviewOutcome:
        if decision not in {"approved", "rejected"}:
            raise conflict(ApiErrorCode.VALIDATION_FAILED, "decision must be approved or rejected")
        if run.hard_failure:
            # A hard failure is a measured fact. No endpoint may erase one, and
            # this is checked before anything else so the refusal is unambiguous.
            raise conflict(
                ApiErrorCode.VALIDATION_FAILED,
                "a hard failure cannot be overridden by human review",
            )
        if run.final_outcome != VisualQAOutcome.REVIEW.value:
            raise conflict(
                ApiErrorCode.VALIDATION_FAILED,
                "only an ambiguous REVIEW result can be resolved by a human",
            )
        existing = next(
            (
                item
                for item in self._repository.human_reviews(run.id)
                if item.idempotency_key == idempotency_key
            ),
            None,
        )
        if existing is not None:
            if existing.decision != decision:
                raise conflict(
                    ApiErrorCode.IDEMPOTENCY_KEY_MISMATCH,
                    "this idempotency key already recorded a different decision",
                )
            return self._outcome(run, existing)
        review = VisualQAHumanReview(
            qa_run_id=run.id,
            expected_row_version=row_version,
            reviewer_principal=self._reviewer,
            decision=decision,
            reason=reason[:MAX_REASON_LENGTH],
            idempotency_key=idempotency_key,
        )
        self._session.add(review)
        self._session.flush()
        return self._outcome(run, review)

    def _outcome(self, run: VisualQARun, review: VisualQAHumanReview) -> HumanReviewOutcome:
        # The automated result is preserved: the gate consults the review, the
        # canonical QA result is never rewritten.
        _, reason = self._repository.gate(run.shot_id, VisualQATargetType(run.target_type))
        return HumanReviewOutcome(
            review_id=review.id,
            qa_run_id=run.id,
            decision=review.decision,
            row_version=review.expected_row_version,
            resulting_gate=reason,
        )


def require_run(session: Session, project_id: UUID, shot_id: UUID, qa_run_id: UUID) -> VisualQARun:
    """Resolve one QA run, returning the same 404 for cross-project IDs."""
    run = VisualQARepository(session).run(project_id, qa_run_id)
    if run is None or run.shot_id != shot_id:
        raise not_found("visual QA run")
    return run


__all__ = [
    "HumanReviewOutcome",
    "ReviewError",
    "VisualQAHumanReviewService",
    "require_run",
]
