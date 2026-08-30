"""Owner-scoped human adjudication of genuinely uncertain T22 findings.

A human can settle a semantic editorial question the system could not resolve -
"does that cut really contradict the script?" - and nothing else. A reviewer can
never clear a deterministic corruption, a stale lineage, a missing required
asset, an invalid timestamp, missing caption or narration coverage, a failing
T20 hard result, or an unresolved T21 human-review state. Those are measured
facts, not judgement calls.

The decision never touches the render or the report. It records who decided,
what they decided, a structured reason, the row version it was decided against,
and when. The gate is then recomputed from the persisted rows: accepting a
review finding removes it from the unresolved set, which may turn ``REVIEW`` into
``PASS``, but it can never turn a ``FAIL`` into anything.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from services.qa.final_rubric import GATE_VERSION
from vidgen.contracts.final_editorial import (
    FinalEditorialReport,
    FinalFindingSeverity,
    FinalQADecision,
    FinalQAStatus,
)
from vidgen.contracts.review import ApiErrorCode
from vidgen.db.final_editorial_models import FinalEditorialRun
from vidgen.db.final_editorial_repository import (
    FinalEditorialConcurrencyError,
    FinalEditorialRepository,
)
from vidgen.db.models import Asset
from vidgen.review.errors import conflict, not_found
from vidgen.storage.blob import BlobStore

MAX_REASON_LENGTH = 1000
DECISIONS = frozenset({"accept", "reject", "escalate"})


@dataclass(frozen=True, slots=True)
class FinalReviewOutcome:
    review_id: UUID
    final_editorial_run_id: UUID
    finding_id: UUID
    decision: str
    row_version: int
    resulting_gate: str


class FinalEditorialHumanReviewService:
    def __init__(self, session: Session, blob_store: BlobStore, reviewer_principal: str) -> None:
        self._session = session
        self._blob = blob_store
        self._reviewer = reviewer_principal
        self._repository = FinalEditorialRepository(session)

    def report(self, run: FinalEditorialRun) -> FinalEditorialReport | None:
        """Load the immutable report this run persisted, if it has one."""
        if run.report_asset_id is None:
            return None
        asset = self._session.get(Asset, run.report_asset_id)
        if asset is None or not self._blob.exists(asset.storage_key):
            return None
        return FinalEditorialReport.model_validate_json(self._blob.read(asset.storage_key))

    def decide(
        self,
        run: FinalEditorialRun,
        *,
        finding_id: UUID,
        decision: str,
        reason_code: str,
        reason: str,
        row_version: int,
        idempotency_key: str,
    ) -> FinalReviewOutcome:
        if decision not in DECISIONS:
            raise conflict(
                ApiErrorCode.VALIDATION_FAILED, "decision must be accept, reject or escalate"
            )
        if not reason.strip():
            raise conflict(
                ApiErrorCode.VALIDATION_FAILED, "a structured reason is required to adjudicate"
            )
        # Deterministic truth is checked first, so the refusal is unambiguous.
        if run.deterministic_failure_count:
            raise conflict(
                ApiErrorCode.VALIDATION_FAILED,
                "a deterministic hard failure cannot be overridden by human review",
            )
        if run.error_code:
            raise conflict(
                ApiErrorCode.VALIDATION_FAILED,
                f"a run that failed input validation ({run.error_code}) cannot be adjudicated",
            )
        if run.status not in {
            FinalQAStatus.FINAL_QA_REVIEW_REQUIRED.value,
            FinalQAStatus.FINAL_QA_ADJUDICATING.value,
        }:
            raise conflict(
                ApiErrorCode.VALIDATION_FAILED,
                "only a run awaiting review may be adjudicated",
            )
        report = self.report(run)
        if report is None:
            raise conflict(
                ApiErrorCode.VALIDATION_FAILED, "the run has no persisted report to adjudicate"
            )
        finding = next((item for item in report.findings if item.finding_id == finding_id), None)
        if finding is None:
            raise not_found("final editorial finding")
        if finding.severity is not FinalFindingSeverity.REVIEW_REQUIRED:
            raise conflict(
                ApiErrorCode.VALIDATION_FAILED,
                "only a review-required semantic finding may be adjudicated",
            )
        if finding.provenance == "deterministic":
            raise conflict(
                ApiErrorCode.VALIDATION_FAILED,
                "a deterministic finding cannot be resolved by human review",
            )
        try:
            review = self._repository.record_review(
                run,
                finding_id=finding_id,
                reviewer_subject=self._reviewer,
                decision=decision,
                reason_code=reason_code,
                reason=reason[:MAX_REASON_LENGTH],
                expected_row_version=row_version,
                idempotency_key=idempotency_key,
            )
        except FinalEditorialConcurrencyError as error:
            raise conflict(ApiErrorCode.VERSION_CONFLICT, str(error)) from error
        gate = self._recompute(run, report)
        self._session.flush()
        return FinalReviewOutcome(
            review_id=review.id,
            final_editorial_run_id=run.id,
            finding_id=finding_id,
            decision=decision,
            row_version=row_version,
            resulting_gate=gate,
        )

    def _recompute(self, run: FinalEditorialRun, report: FinalEditorialReport) -> str:
        """Recompute the gate from persisted rows after a human decision.

        Only the unresolved-review count can move. A blocking finding and a
        deterministic failure are untouched by any decision, so a ``FAIL`` stays
        a ``FAIL`` and the project stays blocked.
        """
        resolved = self._repository.resolved_finding_ids(run.id)
        unresolved = [
            finding
            for finding in report.findings
            if finding.severity is FinalFindingSeverity.REVIEW_REQUIRED
            and finding.finding_id not in resolved
        ]
        if run.blocking_finding_count or run.deterministic_failure_count:
            decision = FinalQADecision.FAIL
        elif unresolved:
            decision = FinalQADecision.REVIEW
        else:
            decision = FinalQADecision.PASS
        run.review_finding_count = len(unresolved)
        run.final_decision = decision.value
        run.status = (
            FinalQAStatus.FINAL_QA_PASSED.value
            if decision is FinalQADecision.PASS
            else FinalQAStatus.FINAL_QA_REVIEW_REQUIRED.value
            if decision is FinalQADecision.REVIEW
            else FinalQAStatus.FINAL_QA_FAILED.value
        )
        self._repository.record_gate(
            run,
            decision=decision,
            blocking_finding_count=run.blocking_finding_count or 0,
            review_finding_count=len(unresolved),
            deterministic_failure_count=run.deterministic_failure_count or 0,
            gate_version=f"{GATE_VERSION}+review:{len(self._repository.reviews(run.id))}",
            reasons=[f"recomputed after human adjudication by {self._reviewer[:64]}"],
        )
        return decision.value


def require_run(
    session: Session, project_id: UUID, final_editorial_run_id: UUID
) -> FinalEditorialRun:
    """Resolve a run inside its project, or raise the same 404 as a missing one."""
    run = FinalEditorialRepository(session).run(project_id, final_editorial_run_id)
    if run is None:
        raise not_found("final editorial run")
    return run


def report_payload(blob: BlobStore, session: Session, run: FinalEditorialRun) -> dict[str, Any]:
    """The persisted report as a plain mapping, or an empty one when absent."""
    if run.report_asset_id is None:
        return {}
    asset = session.get(Asset, run.report_asset_id)
    if asset is None or not blob.exists(asset.storage_key):
        return {}
    return dict(json.loads(blob.read(asset.storage_key)))
