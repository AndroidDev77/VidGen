"""Deterministic T20 score recomputation, outcome and routing recommendation.

Nothing a provider says becomes the canonical score. This module takes validated
dimension proposals plus the deterministic report, rebuilds every weighted
contribution in application code, redistributes the weight of any genuinely
non-applicable dimension, and derives the outcome under one absolute rule: a
technical hard failure (black video, decode failure, duration mismatch, or any
deterministic hard-failure measurement) forces ``FAIL`` regardless of the number.

Semantic hard-failure proposals from the evaluator (identity, count, action,
props, anatomy, continuity) are soft. They block only when the evaluator also
scored that dimension below ``semantic_hard_failure_dimension_floor``; otherwise
the finding is demoted to a warning and the recomputed score decides. Codes in
``warn_only_codes`` are recorded as warnings and never block or route a repair.
When tolerance is the only thing left - every code the shot evidenced is one the
project asked to be told about and not act on - the shot passes below its pass
threshold and the shortfall is recorded as
``vidgen.contracts.visual_qa.TOLERATED_SCORE_BELOW_THRESHOLD``. That marker is
owned by the contract because ``VisualQAResult`` is what admits the pass: both
layers have to agree on the exact string or the verdict cannot be persisted.

The routing recommendation produced here is advisory. T20 never executes it.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any
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
    THRESHOLDS,
)
from vidgen.contracts.visual_qa import (
    TOLERATED_SCORE_BELOW_THRESHOLD,
    VisualQADeterministicReport,
    VisualQADimension,
    VisualQADimensionResult,
    VisualQAEvidence,
    VisualQAFinding,
    VisualQAOutcome,
    VisualQAProviderResult,
    VisualQARepairCode,
    VisualQARepairRecommendation,
    VisualQAResult,
    VisualQARoutingRecommendation,
    VisualQARubric,
    VisualQASample,
    VisualQAScore,
    VisualQAShotImportance,
    VisualQAThresholds,
)
from vidgen.contracts.visual_qa import VisualQADimensionResult as _DimensionResult

#: Warning recorded on a dimension when a semantic hard-failure proposal was
#: demoted because the evaluator's own dimension score did not support it.
HARD_FAILURE_DOWNGRADED_BY_SCORE: str = "hard_failure_downgraded_by_score"

#: Warning recorded when the outcome named more distinct repair codes than
#: ``VisualQAResult`` admits and the lowest-priority codes were dropped.
REPAIR_CODES_TRUNCATED: str = "repair_codes_truncated"

#: Warning recorded when the outcome named more distinct warning codes than
#: ``VisualQAResult`` admits and the lowest-priority warnings were dropped.
WARNING_CODES_TRUNCATED: str = "warning_codes_truncated"

UNKNOWN_PROVIDER_HARD_FAILURE_CODE: str = "unknown_provider_hard_failure_code"
UNEVIDENCED_PROVIDER_HARD_FAILURE_PROPOSAL: str = "unevidenced_provider_hard_failure_proposal"

#: Warnings the contract or a downstream stage reads by exact string. They are
#: kept ahead of everything else when the warning list has to be bounded.
_PROTECTED_WARNINGS: frozenset[str] = frozenset(
    {TOLERATED_SCORE_BELOW_THRESHOLD, REPAIR_CODES_TRUNCATED}
)

#: Warnings scoring itself derives, kept ahead of provider and dimension codes.
_SCORING_WARNINGS: frozenset[str] = frozenset(
    {
        HARD_FAILURE_DOWNGRADED_BY_SCORE,
        UNKNOWN_PROVIDER_HARD_FAILURE_CODE,
        UNEVIDENCED_PROVIDER_HARD_FAILURE_PROPOSAL,
    }
)


def _warn_only_marker(code: VisualQARepairCode) -> str:
    return f"warn_only:{code.value}"


def blocks_regardless_of_score(
    repair_codes: Sequence[VisualQARepairCode],
    raw_score: float,
    thresholds: VisualQAThresholds,
) -> bool:
    """Whether a hard-failure proposal carrying these codes may block the shot.

    A technical code always blocks. A semantic code blocks only when the
    evaluator scored the dimension below the configured floor, so a flag that
    contradicts the evaluator's own number cannot fail a high-scoring shot.
    """
    if any(code in HARD_FAILURE_CODES for code in repair_codes):
        return True
    return raw_score < thresholds.semantic_hard_failure_dimension_floor


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


def _limit(field: str, model: type[Any] = _DimensionResult) -> int:
    """The contract's own bound for one collection, by default a dimension's."""
    metadata = model.model_fields[field].metadata
    for entry in metadata:
        limit = getattr(entry, "max_length", None)
        if limit is not None:
            return int(limit)
    raise KeyError(field)  # pragma: no cover - every bounded field declares one


def _bounded(values: list[Any], field: str) -> list[Any]:
    return values[: _limit(field)]


def _result_limit(field: str) -> int:
    """The bound ``VisualQAResult`` places on one of its code lists.

    Repair codes are also copied into the recommendation, so their bound is the
    tighter of the two collections that carry them.
    """
    limit = _limit(field, VisualQAResult)
    if field == "repair_codes":
        limit = min(limit, _limit(field, VisualQARepairRecommendation))
    return limit


def _retain_repair_codes(
    score: VisualQAScore,
    codes: Iterable[VisualQARepairCode],
    *,
    hard_codes: Iterable[str],
    required: Iterable[VisualQARepairCode],
) -> tuple[list[VisualQARepairCode], bool]:
    """The repair codes a result can carry, and whether any were dropped.

    Each dimension bounds its own codes, but the union across the rubric can
    exceed what ``VisualQAResult`` admits. T21 routes on these codes, so when
    the union has to be cut the codes that matter most are kept, in order:

    1. blocking hard-failure codes, alphabetically - the routed primary code is
       the alphabetically first of them, so it is always retained and the
       recommendation's routing stays consistent with the retained codes;
    2. codes the outcome itself requires (the review markers, or the structural
       family's codes);
    3. the remaining codes by the points their worst evidencing dimension cost
       the score, then alphabetically.

    The retained codes are returned sorted, as an unbounded outcome would be.
    """
    hard = frozenset(hard_codes)
    needed = frozenset(required)
    lost: dict[VisualQARepairCode, float] = {}
    for dimension in score.dimensions:
        shortfall = (100 - dimension.raw_score) * dimension.effective_weight / 100
        for code in dimension.repair_codes:
            lost[code] = max(lost.get(code, 0.0), shortfall)
    ranked = sorted(
        set(codes),
        key=lambda code: (
            code.value not in hard,
            code not in needed,
            -lost.get(code, 0.0),
            code.value,
        ),
    )
    limit = _result_limit("repair_codes")
    return sorted(ranked[:limit], key=lambda code: code.value), len(ranked) > limit


def _warning_rank(code: str) -> tuple[int, str]:
    if code in _PROTECTED_WARNINGS:
        return 0, code
    if code in _SCORING_WARNINGS or code.startswith("warn_only:"):
        return 1, code
    return 2, code


def _retain_warning_codes(codes: Iterable[str]) -> list[str]:
    """The warning codes a result can carry.

    Warnings the contract or a later stage reads by name are kept first, then
    the ones scoring derived (tolerance markers, demotions, rejected provider
    proposals), then provider and dimension codes alphabetically. When anything
    is dropped the last slot records ``WARNING_CODES_TRUNCATED``.
    """
    ranked = sorted(set(codes), key=_warning_rank)
    limit = _result_limit("warning_codes")
    if len(ranked) > limit:
        ranked = [*ranked[: limit - 1], WARNING_CODES_TRUNCATED]
    return sorted(ranked)


def _outcome(
    score: VisualQAScore,
    outcome: VisualQAOutcome,
    *,
    routing: VisualQARoutingRecommendation,
    warning_codes: Iterable[str],
    rationale: str = "",
    hard_codes: Iterable[str] = (),
    repair_codes: Iterable[VisualQARepairCode] = (),
    required: Iterable[VisualQARepairCode] = (),
    review_reasons: Sequence[str] = (),
) -> ScoringOutcome:
    """Build an outcome that ``VisualQAResult`` can always admit.

    Bounding here, rather than when the result is built, keeps the outcome, the
    recommendation and the persisted row in agreement about which codes exist.
    """
    hard = sorted(set(hard_codes))
    warnings = set(warning_codes)
    codes, truncated = _retain_repair_codes(score, repair_codes, hard_codes=hard, required=required)
    # Hard-failure codes are repair-code values, so the taxonomy already keeps
    # them within the result's bound; the slice only guards that invariant.
    hard_limit = _result_limit("hard_failure_codes")
    if truncated or len(hard) > hard_limit:
        warnings.add(REPAIR_CODES_TRUNCATED)
    return ScoringOutcome(
        score=score,
        outcome=outcome,
        hard_failure_codes=tuple(hard[:hard_limit]),
        warning_codes=tuple(_retain_warning_codes(warnings)),
        repair_codes=tuple(codes),
        recommendation=VisualQARepairRecommendation(
            routing=routing,
            repair_codes=codes,
            rationale=rationale,
        ),
        review_reasons=tuple(review_reasons),
    )


def _severity_rank(finding: VisualQAFinding) -> int:
    return {"hard_failure": 0, "warning": 1, "info": 2}[finding.severity]


def _dimension_findings(
    provider: VisualQAProviderResult,
    dimension: VisualQADimension,
    samples: Sequence[VisualQASample],
    evaluator: str,
    *,
    raw_score: float,
    thresholds: VisualQAThresholds,
) -> tuple[list[VisualQAFinding], bool]:
    """Rebuild one dimension's provider findings; the flag says one was demoted."""
    by_id = {sample.sample_id: sample for sample in samples}
    findings: list[VisualQAFinding] = []
    demoted = False
    for ordinal, item in enumerate(provider.findings):
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
                    finding_code=f"{item.code}:{ordinal}",
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
        if severity == "hard_failure" and not blocks_regardless_of_score(
            repair_codes, raw_score, thresholds
        ):
            # A semantic hard-failure flag on a dimension the evaluator itself
            # scored at or above the floor contradicts its own number. The
            # number wins: the finding, its evidence and its repair codes are
            # kept as a warning so nothing is lost, but it cannot block.
            severity = "warning"
            demoted = True
        findings.append(
            VisualQAFinding(
                # The ordinal keeps two findings that share a dimension, a code
                # and a cited frame from collapsing onto one primary key.
                finding_id=deterministic_id(
                    "finding", evaluator, dimension.value, item.code, ordinal
                ),
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
    return findings, demoted


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
    thresholds: VisualQAThresholds | None = None,
) -> list[VisualQADimensionResult]:
    """Recompute every dimension from validated provider and deterministic input."""
    thresholds = THRESHOLDS if thresholds is None else thresholds
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
        raw = float(proposal.raw_score) if proposal else 0.0
        findings, demoted = _dimension_findings(
            provider,
            dimension,
            samples,
            provider.model,
            raw_score=raw,
            thresholds=thresholds,
        )
        findings.extend(_deterministic_findings(report, dimension, samples, source_asset_id))
        findings.sort(key=_severity_rank)
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
                    # Bounded on this branch too: a provider is free to report
                    # findings for a dimension it marked non-applicable, and an
                    # unbounded list would fail the whole run on validation.
                    findings=_bounded(findings, "findings"),
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
        hard_codes = sorted(
            {
                code
                for finding in findings
                if finding.severity == "hard_failure"
                for code in finding.repair_codes
            }
        )
        warning_codes = sorted(
            {
                *(finding.code for finding in findings if finding.severity == "warning"),
                *([HARD_FAILURE_DOWNGRADED_BY_SCORE] if demoted else []),
            }
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
                # A provider may return more findings than one dimension result
                # may carry. Blocking findings are kept first, so truncation can
                # never drop the evidence that fails the shot.
                findings=_bounded(findings, "findings"),
                warning_codes=_bounded(warning_codes, "warning_codes"),
                hard_failure_codes=_bounded(
                    [code.value for code in hard_codes], "hard_failure_codes"
                ),
                repair_codes=_bounded(repair_codes, "repair_codes"),
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
    # The structural code is only added when it names the family being
    # recommended. A new-seed route does not imply an over-complex prompt, so
    # PROMPT_TOO_COMPLEX is no longer a catch-all on every structural failure.
    if routing is VisualQARoutingRecommendation.COMPOSITION_SPLIT:
        extra.append(VisualQARepairCode.TOO_MANY_CHARACTERS)
    elif routing is VisualQARoutingRecommendation.PROMPT_SIMPLIFICATION:
        extra.append(VisualQARepairCode.PROMPT_TOO_COMPLEX)
    return routing, extra


def decide(
    score: VisualQAScore,
    report: VisualQADeterministicReport,
    provider: VisualQAProviderResult,
    *,
    thresholds: VisualQAThresholds,
    review_reasons: Sequence[str] = (),
    warn_only_codes: Iterable[str] | None = None,
) -> ScoringOutcome:
    """Derive the canonical outcome, repair codes and routing recommendation.

    ``warn_only_codes`` names the repair codes this deployment or project
    tolerates: they are still recorded, as ``warn_only:<CODE>`` warnings, but
    never block the shot and are never handed to T21 as a repair. Unset, the
    thresholds' own set applies.
    """
    warn_only = frozenset(
        thresholds.warn_only_codes if warn_only_codes is None else warn_only_codes
    )
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
            warning_codes.add(UNKNOWN_PROVIDER_HARD_FAILURE_CODE)
            continue
        if code in HARD_FAILURE_CODES and code in evidenced:
            hard_codes.add(code.value)
            repair_codes.add(code)
        else:
            warning_codes.add(UNEVIDENCED_PROVIDER_HARD_FAILURE_PROPOSAL)
    # A tolerated code is measured and visible, but it neither blocks nor buys
    # a repair attempt: it moves from the hard and repair sets to the warnings.
    tolerated_repair_codes: set[VisualQARepairCode] = set()
    for code in sorted(repair_codes, key=lambda code: code.value):
        if code.value in warn_only:
            repair_codes.discard(code)
            hard_codes.discard(code.value)
            tolerated_repair_codes.add(code)
            warning_codes.add(_warn_only_marker(code))
    hard_codes.difference_update(warn_only)
    if hard_codes:
        primary = min(
            (code for code in repair_codes if code.value in hard_codes),
            key=lambda code: code.value,
            default=None,
        )
        routing = (
            REPAIR_CODES[primary].repair_family
            if primary is not None
            else VisualQARoutingRecommendation.TARGETED_REPAIR
        )
        return _outcome(
            score,
            VisualQAOutcome.FAIL,
            routing=routing,
            rationale="a hard failure blocks the shot regardless of the numeric score",
            hard_codes=hard_codes,
            repair_codes=repair_codes,
            warning_codes=warning_codes,
            review_reasons=review_reasons,
        )
    if review_reasons:
        markers = (
            VisualQARepairCode.HUMAN_REVIEW_REQUIRED,
            VisualQARepairCode.AMBIGUOUS_VISUAL_EVIDENCE,
        )
        return _outcome(
            score,
            VisualQAOutcome.REVIEW,
            routing=VisualQARoutingRecommendation.HUMAN_REVIEW,
            rationale="; ".join(review_reasons)[:500],
            repair_codes={*repair_codes, *markers},
            required=markers,
            warning_codes=warning_codes,
            review_reasons=review_reasons,
        )
    if score.total >= score.pass_threshold:
        return _outcome(
            score,
            VisualQAOutcome.PASS,
            routing=VisualQARoutingRecommendation.NONE,
            warning_codes=warning_codes,
        )
    if score.total >= thresholds.targeted_repair_floor:
        codes = sorted(repair_codes, key=lambda code: code.value)
        if not codes:
            worst = min(
                (item for item in score.dimensions if item.applicable),
                key=lambda item: item.raw_score,
            )
            derived = DIMENSION_DEFAULT_REPAIR[worst.dimension]
            # Nothing is left to repair: either every code the shot evidenced
            # is tolerated, or the code the worst dimension would name is. A
            # repair would be handed exactly the codes this project asked to
            # be told about and not act on, so the shot passes on tolerance and
            # the low score is recorded as a warning instead.
            if tolerated_repair_codes or derived.value in warn_only:
                warning_codes.add(TOLERATED_SCORE_BELOW_THRESHOLD)
                if derived.value in warn_only:
                    warning_codes.add(_warn_only_marker(derived))
                return _outcome(
                    score,
                    VisualQAOutcome.PASS,
                    routing=VisualQARoutingRecommendation.NONE,
                    warning_codes=warning_codes,
                )
            codes = [derived]
        return _outcome(
            score,
            VisualQAOutcome.FAIL,
            routing=VisualQARoutingRecommendation.TARGETED_REPAIR,
            rationale=(
                f"score {score.total:.2f} is below the {score.pass_threshold:.0f} threshold "
                f"but at or above the {thresholds.targeted_repair_floor:.0f} repair floor"
            ),
            repair_codes=codes,
            warning_codes=warning_codes,
        )
    routing, extra = _structural_routing(score.dimensions)
    tolerated = [code for code in extra if code.value in warn_only]
    warning_codes.update(_warn_only_marker(code) for code in tolerated)
    structural = [code for code in extra if code not in tolerated]
    if not structural:
        # Every structural code was tolerated; the family's default still names
        # the dimension that failed so the result carries a repair code.
        structural = [] if repair_codes else [extra[0]]
    return _outcome(
        score,
        VisualQAOutcome.FAIL,
        routing=routing,
        rationale=(
            f"score {score.total:.2f} is below the "
            f"{thresholds.targeted_repair_floor:.0f} targeted-repair floor"
        ),
        repair_codes={*repair_codes, *structural},
        required=structural,
        warning_codes=warning_codes,
    )
