"""Deterministic T20 score recomputation, outcome and routing recommendation.

Nothing a provider says becomes the canonical score. This module takes validated
dimension proposals plus the deterministic report, rebuilds every weighted
contribution in application code, redistributes the weight of any genuinely
non-applicable dimension, and derives the outcome under one absolute rule: a
hard failure forces ``FAIL`` regardless of the number.

The routing recommendation produced here is advisory. T20 never executes it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from services.qa.evidence import (
    deterministic_id,
    frame_evidence,
    measurement_evidence,
    nearest_sample,
)
from services.qa.rubric import (
    DIMENSION_DEFAULT_REPAIR,
    DIMENSION_STRUCTURAL_ROUTING,
    HARD_FAILURE_CODES,
    REPAIR_CODE_DIMENSIONS,
    REPAIR_CODES,
)
from vidgen.contracts.visual_qa import (
    VisualQADeterministicReport,
    VisualQADimension,
    VisualQADimensionResult,
    VisualQAEvidence,
    VisualQAFinding,
    VisualQAOutcome,
    VisualQAProviderResult,
    VisualQARepairCode,
    VisualQARepairRecommendation,
    VisualQARoutingRecommendation,
    VisualQARubric,
    VisualQASample,
    VisualQAScore,
    VisualQAShotImportance,
    VisualQAThresholds,
)


@dataclass(frozen=True, slots=True)
class ScoringOutcome:
    """The canonical recomputed score plus everything derived from it."""

    score: VisualQAScore
    outcome: VisualQAOutcome
    hard_failure_codes: tuple[str, ...]
    warning_codes: tuple[str, ...]
    repair_codes: tuple[VisualQARepairCode, ...]
    recommendation: VisualQARepairRecommendation
    review_reasons: tuple[str, ...]

    @property
    def hard_failure(self) -> bool:
        return bool(self.hard_failure_codes)


def _dimension_findings(
    provider: VisualQAProviderResult,
    dimension: VisualQADimension,
    samples: Sequence[VisualQASample],
    evaluator: str,
) -> list[VisualQAFinding]:
    by_id = {sample.sample_id: sample for sample in samples}
    findings: list[VisualQAFinding] = []
    for item in provider.findings:
        if item.dimension is not dimension:
            continue
        evidence: list[VisualQAEvidence] = []
        for sample_id in item.sample_ids:
            sample = by_id.get(sample_id)
            if sample is None:
                continue
            evidence.append(
                frame_evidence(
                    sample,
                    explanation=item.summary,
                    confidence=item.confidence,
                    bounding_box=item.bounding_box,
                    compared_reference_asset_id=item.compared_reference_asset_id,
                    finding_code=item.code,
                )
            )
        severity = item.severity
        if severity != "info" and not evidence:
            # A finding whose evidence did not resolve to a real sample cannot
            # block a shot; it is demoted rather than trusted.
            severity = "info"
        repair_codes = list(item.repair_codes)
        if severity == "hard_failure" and not repair_codes:
            repair_codes = [DIMENSION_DEFAULT_REPAIR[dimension]]
        findings.append(
            VisualQAFinding(
                finding_id=deterministic_id("finding", evaluator, dimension.value, item.code),
                dimension=dimension,
                severity=severity,
                code=item.code,
                summary=item.summary,
                proposed_correction=item.proposed_correction,
                repair_codes=repair_codes,
                confidence=item.confidence,
                evidence=evidence,
            )
        )
    return findings


def _deterministic_findings(
    report: VisualQADeterministicReport,
    dimension: VisualQADimension,
    samples: Sequence[VisualQASample],
    source_asset_id: UUID,
) -> list[VisualQAFinding]:
    findings: list[VisualQAFinding] = []
    for metric in report.metrics:
        if metric.outcome not in {"warning", "hard_failure"} or metric.repair_code is None:
            continue
        if REPAIR_CODE_DIMENSIONS.get(metric.repair_code) is not dimension:
            continue
        sample = nearest_sample(samples, metric.evidence_timestamp_us)
        evidence = measurement_evidence(
            sample,
            source_asset_id=source_asset_id,
            code=metric.code,
            measurement=metric.measurement,
            explanation=metric.message or metric.diagnostic_code,
            timestamp_us=metric.evidence_timestamp_us,
        )
        findings.append(
            VisualQAFinding(
                finding_id=deterministic_id("deterministic", metric.code, metric.diagnostic_code),
                dimension=dimension,
                severity=metric.outcome,  # type: ignore[arg-type]
                code=metric.diagnostic_code,
                summary=metric.message or f"{metric.code} outside the configured threshold",
                proposed_correction="",
                repair_codes=[metric.repair_code],
                confidence=1.0,
                evidence=[evidence],
            )
        )
    return findings


def build_dimension_results(
    provider: VisualQAProviderResult,
    report: VisualQADeterministicReport,
    *,
    rubric: VisualQARubric,
    samples: Sequence[VisualQASample],
    source_asset_id: UUID,
) -> list[VisualQADimensionResult]:
    """Recompute every dimension from validated provider and deterministic input."""
    proposals = {item.dimension: item for item in provider.dimension_scores}
    applicable: dict[VisualQADimension, bool] = {}
    for entry in rubric.dimensions:
        proposal = proposals.get(entry.dimension)
        applicable[entry.dimension] = proposal.applicable if proposal else False
    # Deterministic hard failures always apply, whatever the provider claimed.
    for finding_dimension in {
        REPAIR_CODE_DIMENSIONS[metric.repair_code]
        for metric in report.metrics
        if metric.repair_code is not None and metric.outcome == "hard_failure"
    }:
        applicable[finding_dimension] = True
    if not any(applicable.values()):
        raise ValueError("every rubric dimension was reported non-applicable")
    total_applicable_weight = sum(
        rubric.weight_for(dimension) for dimension, ok in applicable.items() if ok
    )
    results: list[VisualQADimensionResult] = []
    for entry in rubric.dimensions:
        dimension = entry.dimension
        proposal = proposals.get(dimension)
        findings = _dimension_findings(provider, dimension, samples, provider.model)
        findings.extend(_deterministic_findings(report, dimension, samples, source_asset_id))
        if not applicable[dimension]:
            results.append(
                VisualQADimensionResult(
                    dimension=dimension,
                    applicable=False,
                    raw_score=0.0,
                    weight=entry.weight,
                    effective_weight=0.0,
                    weighted_contribution=0.0,
                    confidence=proposal.confidence if proposal else 0.0,
                    findings=findings,
                    evaluator=provider.provider,
                    model=provider.model,
                    rubric_version=rubric.rubric_version,
                )
            )
            continue
        # Documented redistribution: a genuinely non-applicable dimension gives
        # its weight to the applicable ones in proportion, so an absent dimension
        # can never hand the shot free credit.
        effective = entry.weight * 100 / total_applicable_weight
        raw = float(proposal.raw_score) if proposal else 0.0
        hard_codes = sorted(
            {
                code
                for finding in findings
                if finding.severity == "hard_failure"
                for code in finding.repair_codes
            }
        )
        warning_codes = sorted(
            {finding.code for finding in findings if finding.severity == "warning"}
        )
        repair_codes = sorted(
            {code for finding in findings for code in finding.repair_codes},
            key=lambda code: code.value,
        )
        results.append(
            VisualQADimensionResult(
                dimension=dimension,
                applicable=True,
                raw_score=raw,
                weight=entry.weight,
                effective_weight=effective,
                weighted_contribution=raw * effective / 100,
                confidence=proposal.confidence if proposal else 0.0,
                findings=findings,
                warning_codes=warning_codes,
                hard_failure_codes=[code.value for code in hard_codes],
                repair_codes=repair_codes,
                evaluator=provider.provider,
                model=provider.model,
                rubric_version=rubric.rubric_version,
            )
        )
    return results


def recompute(
    dimensions: Sequence[VisualQADimensionResult],
    *,
    rubric: VisualQARubric,
    thresholds: VisualQAThresholds,
    importance: VisualQAShotImportance,
) -> VisualQAScore:
    """Recompute the canonical weighted total in application code."""
    applicable = [item for item in dimensions if item.applicable]
    if not applicable:
        raise ValueError("at least one rubric dimension must be applicable")
    weight_total = sum(item.effective_weight for item in applicable)
    total = sum(item.weighted_contribution for item in applicable)
    confidence = (
        sum(item.confidence * item.effective_weight for item in applicable) / weight_total
        if weight_total
        else 0.0
    )
    return VisualQAScore(
        rubric_version=rubric.rubric_version,
        threshold_version=thresholds.threshold_version,
        importance=importance,
        pass_threshold=thresholds.pass_score(importance),
        total=min(100.0, max(0.0, total)),
        applied_weight_total=weight_total,
        dimensions=list(dimensions),
        confidence=min(1.0, max(0.0, confidence)),
    )


def _structural_routing(
    dimensions: Sequence[VisualQADimensionResult],
) -> tuple[VisualQARoutingRecommendation, list[VisualQARepairCode]]:
    """Choose the structural repair family from the worst applicable dimensions."""
    applicable = sorted(
        (item for item in dimensions if item.applicable), key=lambda item: item.raw_score
    )
    if not applicable:
        return VisualQARoutingRecommendation.PROMPT_SIMPLIFICATION, [
            VisualQARepairCode.PROMPT_TOO_COMPLEX
        ]
    worst = applicable[0]
    routing = DIMENSION_STRUCTURAL_ROUTING[worst.dimension]
    extra: list[VisualQARepairCode] = [DIMENSION_DEFAULT_REPAIR[worst.dimension]]
    if routing is VisualQARoutingRecommendation.COMPOSITION_SPLIT:
        extra.append(VisualQARepairCode.TOO_MANY_CHARACTERS)
    else:
        extra.append(VisualQARepairCode.PROMPT_TOO_COMPLEX)
    return routing, extra


def decide(
    score: VisualQAScore,
    report: VisualQADeterministicReport,
    provider: VisualQAProviderResult,
    *,
    thresholds: VisualQAThresholds,
    review_reasons: Sequence[str] = (),
) -> ScoringOutcome:
    """Derive the canonical outcome, repair codes and routing recommendation."""
    hard_codes: set[str] = set()
    repair_codes: set[VisualQARepairCode] = set()
    warning_codes: set[str] = set()
    for dimension in score.dimensions:
        hard_codes.update(dimension.hard_failure_codes)
        repair_codes.update(dimension.repair_codes)
        warning_codes.update(dimension.warning_codes)
    for metric in report.metrics:
        if metric.outcome == "warning":
            warning_codes.add(metric.diagnostic_code)
        if metric.outcome == "hard_failure" and metric.repair_code is not None:
            hard_codes.add(metric.repair_code.value)
            repair_codes.add(metric.repair_code)
    warning_codes.update(provider.warning_codes)
    # A provider may propose a hard failure, but only a code in the bounded
    # taxonomy that is a hard failure and that a dimension actually evidenced
    # can block the shot.
    evidenced = {code for dimension in score.dimensions for code in dimension.repair_codes}
    for raw in provider.proposed_hard_failure_codes:
        try:
            code = VisualQARepairCode(raw)
        except ValueError:
            warning_codes.add("unknown_provider_hard_failure_code")
            continue
        if code in HARD_FAILURE_CODES and code in evidenced:
            hard_codes.add(code.value)
            repair_codes.add(code)
        else:
            warning_codes.add("unevidenced_provider_hard_failure_proposal")
    if hard_codes:
        codes = sorted(repair_codes, key=lambda code: code.value)
        primary = min(
            (code for code in codes if code.value in hard_codes),
            key=lambda code: code.value,
            default=None,
        )
        routing = (
            REPAIR_CODES[primary].repair_family
            if primary is not None
            else VisualQARoutingRecommendation.TARGETED_REPAIR
        )
        return ScoringOutcome(
            score=score,
            outcome=VisualQAOutcome.FAIL,
            hard_failure_codes=tuple(sorted(hard_codes)),
            warning_codes=tuple(sorted(warning_codes)),
            repair_codes=tuple(codes),
            recommendation=VisualQARepairRecommendation(
                routing=routing,
                repair_codes=codes,
                rationale="a hard failure blocks the shot regardless of the numeric score",
            ),
            review_reasons=tuple(review_reasons),
        )
    if review_reasons:
        codes = sorted(
            {
                *repair_codes,
                VisualQARepairCode.HUMAN_REVIEW_REQUIRED,
                VisualQARepairCode.AMBIGUOUS_VISUAL_EVIDENCE,
            },
            key=lambda code: code.value,
        )
        return ScoringOutcome(
            score=score,
            outcome=VisualQAOutcome.REVIEW,
            hard_failure_codes=(),
            warning_codes=tuple(sorted(warning_codes)),
            repair_codes=tuple(codes),
            recommendation=VisualQARepairRecommendation(
                routing=VisualQARoutingRecommendation.HUMAN_REVIEW,
                repair_codes=codes,
                rationale="; ".join(review_reasons)[:500],
            ),
            review_reasons=tuple(review_reasons),
        )
    if score.total >= score.pass_threshold:
        return ScoringOutcome(
            score=score,
            outcome=VisualQAOutcome.PASS,
            hard_failure_codes=(),
            warning_codes=tuple(sorted(warning_codes)),
            repair_codes=(),
            recommendation=VisualQARepairRecommendation(
                routing=VisualQARoutingRecommendation.NONE,
                repair_codes=[],
                rationale="",
            ),
            review_reasons=(),
        )
    if score.total >= thresholds.targeted_repair_floor:
        codes = sorted(repair_codes, key=lambda code: code.value)
        if not codes:
            worst = min(
                (item for item in score.dimensions if item.applicable),
                key=lambda item: item.raw_score,
            )
            codes = [DIMENSION_DEFAULT_REPAIR[worst.dimension]]
        return ScoringOutcome(
            score=score,
            outcome=VisualQAOutcome.FAIL,
            hard_failure_codes=(),
            warning_codes=tuple(sorted(warning_codes)),
            repair_codes=tuple(codes),
            recommendation=VisualQARepairRecommendation(
                routing=VisualQARoutingRecommendation.TARGETED_REPAIR,
                repair_codes=codes,
                rationale=(
                    f"score {score.total:.2f} is below the {score.pass_threshold:.0f} threshold "
                    f"but at or above the {thresholds.targeted_repair_floor:.0f} repair floor"
                ),
            ),
            review_reasons=(),
        )
    routing, extra = _structural_routing(score.dimensions)
    codes = sorted({*repair_codes, *extra}, key=lambda code: code.value)
    return ScoringOutcome(
        score=score,
        outcome=VisualQAOutcome.FAIL,
        hard_failure_codes=(),
        warning_codes=tuple(sorted(warning_codes)),
        repair_codes=tuple(codes),
        recommendation=VisualQARepairRecommendation(
            routing=routing,
            repair_codes=codes,
            rationale=(
                f"score {score.total:.2f} is below the "
                f"{thresholds.targeted_repair_floor:.0f} targeted-repair floor"
            ),
        ),
        review_reasons=(),
    )
