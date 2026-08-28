"""Bounded T20 adjudication.

Adjudication is a second, independent evaluation - the design's *Terra* role -
requested only when the first pass cannot be trusted on its own. It is bounded:
at most the configured number of attempts, never a loop. Both the original and
the adjudicated results are persisted, and the canonical score is still
recomputed by application code from the adjudicated dimensions.

If the adjudicator is not confident enough to decide, the outcome is ``REVIEW``.
The system never manufactures certainty it does not have.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from services.qa.rubric import ADJUDICATION_POLICY_VERSION, REPAIR_CODE_DIMENSIONS
from services.qa.scoring import ScoringOutcome
from vidgen.contracts.visual_qa import (
    VisualQAAdjudication,
    VisualQADeterministicReport,
    VisualQADimension,
    VisualQAOutcome,
    VisualQAProviderResult,
    VisualQAThresholds,
)

#: Dimensions whose low confidence must escalate rather than pass silently.
ESCALATING_DIMENSIONS = (
    VisualQADimension.CHARACTER_IDENTITY,
    VisualQADimension.CONTINUITY_AND_STYLE,
)
#: A provider score this high on a dimension the deterministic checks flagged is
#: a material disagreement, not a rounding difference.
MATERIAL_DISAGREEMENT_SCORE = 85.0


@dataclass(frozen=True, slots=True)
class AdjudicationTriggers:
    """Why adjudication is required, and what the adjudicator must resolve."""

    reasons: tuple[str, ...]
    disagreements: tuple[str, ...]

    def __bool__(self) -> bool:
        return bool(self.reasons)


def evaluate_triggers(
    first_pass: VisualQAProviderResult,
    report: VisualQADeterministicReport,
    outcome: ScoringOutcome,
    *,
    thresholds: VisualQAThresholds,
    ambiguity_reasons: Sequence[str] = (),
    prior_outcome: VisualQAOutcome | None = None,
) -> AdjudicationTriggers:
    """Decide whether the first pass can stand on its own."""
    reasons: list[str] = []
    disagreements: list[str] = []
    scores = {item.dimension: item for item in first_pass.dimension_scores}
    for dimension in ESCALATING_DIMENSIONS:
        proposal = scores.get(dimension)
        if proposal is not None and proposal.confidence < thresholds.adjudication_confidence_floor:
            reasons.append(
                f"first-pass {dimension.value} confidence {proposal.confidence:.2f} is below "
                f"{thresholds.adjudication_confidence_floor:.2f}"
            )
    flagged = {
        REPAIR_CODE_DIMENSIONS[metric.repair_code]
        for metric in report.metrics
        if metric.repair_code is not None and metric.outcome in {"warning", "hard_failure"}
    }
    for dimension in sorted(flagged, key=lambda item: item.value):
        proposal = scores.get(dimension)
        if proposal is not None and proposal.raw_score >= MATERIAL_DISAGREEMENT_SCORE:
            message = (
                f"deterministic checks flagged {dimension.value} while the first pass scored "
                f"{proposal.raw_score:.0f}"
            )
            reasons.append(message)
            disagreements.append(message)
    if "unevidenced_provider_hard_failure_proposal" in outcome.warning_codes:
        message = "a hard failure was proposed without evidence the pipeline could resolve"
        reasons.append(message)
        disagreements.append(message)
    margin = abs(outcome.score.total - outcome.score.pass_threshold)
    if margin <= thresholds.near_threshold_margin:
        reasons.append(
            f"recomputed score {outcome.score.total:.2f} is within "
            f"{thresholds.near_threshold_margin:.1f} of the {outcome.score.pass_threshold:.0f} "
            "pass threshold"
        )
    for ambiguity in ambiguity_reasons:
        reasons.append(ambiguity)
    if prior_outcome is not None and prior_outcome is not outcome.outcome:
        message = (
            f"a prior QA result for this target concluded {prior_outcome.value} while this pass "
            f"concluded {outcome.outcome.value}"
        )
        reasons.append(message)
        disagreements.append(message)
    return AdjudicationTriggers(
        reasons=tuple(dict.fromkeys(reasons))[:8],
        disagreements=tuple(dict.fromkeys(disagreements))[:8],
    )


def resolve(
    *,
    adjudication_id: UUID,
    triggers: AdjudicationTriggers,
    first_pass: VisualQAProviderResult,
    adjudicator: VisualQAProviderResult,
    adjudicated_outcome: ScoringOutcome,
    thresholds: VisualQAThresholds,
    attempts_used: int,
) -> tuple[VisualQAAdjudication, VisualQAOutcome, tuple[str, ...]]:
    """Apply the bounded adjudication policy to the adjudicated evaluation.

    Terra decides only at or above the configured decision confidence. Below it
    the shot goes to ``REVIEW`` with the reasons preserved, unless a hard failure
    already blocks it - a hard failure is never softened by low confidence.
    """
    decided = adjudicator.overall_confidence >= thresholds.adjudication_decision_confidence
    if adjudicated_outcome.hard_failure:
        outcome = VisualQAOutcome.FAIL
        review_reasons: tuple[str, ...] = ()
    elif decided:
        outcome = adjudicated_outcome.outcome
        review_reasons = ()
    else:
        outcome = VisualQAOutcome.REVIEW
        review_reasons = (
            *triggers.reasons,
            f"adjudicator confidence {adjudicator.overall_confidence:.2f} is below the "
            f"{thresholds.adjudication_decision_confidence:.2f} decision threshold",
        )
    record = VisualQAAdjudication(
        adjudication_id=adjudication_id,
        policy_version=ADJUDICATION_POLICY_VERSION,
        triggered_by=list(triggers.reasons) or ["adjudication requested"],
        first_pass_provider=first_pass.provider,
        first_pass_model=first_pass.model,
        adjudicator_provider=adjudicator.provider,
        adjudicator_model=adjudicator.model,
        adjudicator_confidence=adjudicator.overall_confidence,
        decided=decided,
        disagreement_summary=list(triggers.disagreements),
        resulting_outcome_hint=outcome,
        attempts_used=max(1, attempts_used),
    )
    return record, outcome, review_reasons[:8]
