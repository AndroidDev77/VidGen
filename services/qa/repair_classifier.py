"""Deterministic T21 failure classification.

The classifier answers exactly one question: *why* did this shot fail? It reads
the persisted T20 result - repair codes, findings, confidence, the hard-failure
flag, the recomputed score and the rubric dimensions - plus any technical signal
raised outside QA, and produces one
:class:`~vidgen.contracts.repair.RepairClassification`.

It never re-decides whether the shot passed. T20 owns that: the score, the
threshold and ``hard_failure`` are copied through unchanged, and the only thing
derived from them here is *how much* has to change.

The score bands mirror T20 exactly:

============================  ==========================================
score                          severity
============================  ==========================================
at or above the threshold      not repaired at all (T20 already passed it)
75 to below the threshold      targeted repair
below 75                       structural repair
============================  ==========================================

A hard failure always fails regardless of score, so it always earns at least a
targeted repair, and a hard failure in a categorical dimension - the wrong
person, the wrong place, the wrong number of people - is always structural.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from vidgen.contracts.repair import (
    RepairClassification,
    RepairDiagnostic,
    RepairDiagnosticCode,
    RepairFailureCategory,
    RepairSeverity,
)
from vidgen.contracts.visual_qa import (
    VisualQADimension,
    VisualQAFinding,
    VisualQAOutcome,
    VisualQARepairCode,
    VisualQAResult,
)

CLASSIFIER_VERSION = "t21-repair-classifier/1.0"
TARGETED_REPAIR_FLOOR = 75.0

Code = RepairDiagnosticCode

#: Every T20 repair code maps to exactly one T21 diagnostic. A code with no
#: entry is a contract drift and raises rather than being silently ignored.
REPAIR_CODE_DIAGNOSTICS: dict[VisualQARepairCode, RepairDiagnosticCode] = {
    VisualQARepairCode.WRONG_CHARACTER_IDENTITY: Code.WRONG_CHARACTER_IDENTITY,
    VisualQARepairCode.MISSING_PRIMARY_CHARACTER: Code.WRONG_CHARACTER_IDENTITY,
    VisualQARepairCode.EXTRA_CHARACTER: Code.WRONG_CHARACTER_COUNT,
    VisualQARepairCode.WRONG_CHARACTER_COUNT: Code.WRONG_CHARACTER_COUNT,
    VisualQARepairCode.TOO_MANY_CHARACTERS: Code.WRONG_CHARACTER_COUNT,
    VisualQARepairCode.WRONG_WARDROBE: Code.WRONG_WARDROBE_OR_STATE,
    VisualQARepairCode.WRONG_CHARACTER_STATE: Code.WRONG_WARDROBE_OR_STATE,
    VisualQARepairCode.WRONG_LOCATION: Code.WRONG_LOCATION,
    VisualQARepairCode.WRONG_LOCATION_STATE: Code.WRONG_LOCATION,
    VisualQARepairCode.MISSING_REQUIRED_PROP: Code.MISSING_OR_INCORRECT_ACTION,
    VisualQARepairCode.WRONG_PROP_OWNERSHIP: Code.MISSING_OR_INCORRECT_ACTION,
    VisualQARepairCode.MISSING_MANDATORY_ACTION: Code.MISSING_OR_INCORRECT_ACTION,
    VisualQARepairCode.WRONG_ACTION: Code.MISSING_OR_INCORRECT_ACTION,
    VisualQARepairCode.EXCESSIVE_MOTION: Code.MISSING_OR_INCORRECT_ACTION,
    VisualQARepairCode.INSUFFICIENT_MOTION: Code.WEAK_MOTION,
    VisualQARepairCode.EXCESSIVE_FREEZE: Code.WEAK_MOTION,
    VisualQARepairCode.CAMERA_PLAN_MISMATCH: Code.COMPOSITION_FAILURE,
    VisualQARepairCode.COMPOSITION_MISMATCH: Code.COMPOSITION_FAILURE,
    VisualQARepairCode.SCREEN_DIRECTION_CONTRADICTION: Code.COMPOSITION_FAILURE,
    VisualQARepairCode.FACE_BREAKAGE: Code.ANATOMY_OR_ARTIFACT_FAILURE,
    VisualQARepairCode.ANATOMY_BREAKAGE: Code.ANATOMY_OR_ARTIFACT_FAILURE,
    VisualQARepairCode.UNINTENDED_TEXT: Code.ANATOMY_OR_ARTIFACT_FAILURE,
    VisualQARepairCode.EXCESSIVE_FLICKER: Code.ANATOMY_OR_ARTIFACT_FAILURE,
    VisualQARepairCode.STYLE_DRIFT: Code.STYLE_MISMATCH,
    VisualQARepairCode.CONTINUITY_BREAK: Code.CONTINUITY_FAILURE,
    VisualQARepairCode.BLACK_VIDEO: Code.CORRUPT_OR_INCOMPLETE_MEDIA,
    VisualQARepairCode.DECODE_FAILURE: Code.CORRUPT_OR_INCOMPLETE_MEDIA,
    VisualQARepairCode.DURATION_MISMATCH: Code.IMPOSSIBLE_DURATION_OR_MOTION,
    VisualQARepairCode.PROMPT_TOO_COMPLEX: Code.PROMPT_OVERCONSTRAINT,
    VisualQARepairCode.TOO_MANY_REFERENCES: Code.PROMPT_OVERCONSTRAINT,
    VisualQARepairCode.AMBIGUOUS_VISUAL_EVIDENCE: Code.PROMPT_AMBIGUITY,
    VisualQARepairCode.HUMAN_REVIEW_REQUIRED: Code.PROMPT_AMBIGUITY,
}

DIAGNOSTIC_CATEGORIES: dict[RepairDiagnosticCode, RepairFailureCategory] = {
    Code.WRONG_CHARACTER_IDENTITY: RepairFailureCategory.PROMPT_ISSUE,
    Code.WRONG_CHARACTER_COUNT: RepairFailureCategory.PROMPT_ISSUE,
    Code.WRONG_LOCATION: RepairFailureCategory.PROMPT_ISSUE,
    Code.WRONG_WARDROBE_OR_STATE: RepairFailureCategory.PROMPT_ISSUE,
    Code.MISSING_OR_INCORRECT_ACTION: RepairFailureCategory.PROMPT_ISSUE,
    Code.CONTINUITY_FAILURE: RepairFailureCategory.PROMPT_ISSUE,
    Code.STYLE_MISMATCH: RepairFailureCategory.PROMPT_ISSUE,
    Code.PROMPT_OVERCONSTRAINT: RepairFailureCategory.PROMPT_ISSUE,
    Code.PROMPT_AMBIGUITY: RepairFailureCategory.PROMPT_ISSUE,
    # Stochastic sampling failures: the prompt was right and the draw was not.
    Code.WEAK_MOTION: RepairFailureCategory.SEED_ISSUE,
    Code.ANATOMY_OR_ARTIFACT_FAILURE: RepairFailureCategory.SEED_ISSUE,
    Code.COMPOSITION_FAILURE: RepairFailureCategory.SEED_ISSUE,
    Code.REFERENCE_CONFLICT: RepairFailureCategory.REFERENCE_ISSUE,
    Code.PROVIDER_SAFETY_REJECTION: RepairFailureCategory.PROVIDER_ISSUE,
    Code.PROVIDER_TIMEOUT_OR_SERVICE_FAILURE: RepairFailureCategory.PROVIDER_ISSUE,
    Code.UNSUPPORTED_PROVIDER_CAPABILITY: RepairFailureCategory.PROVIDER_ISSUE,
    Code.CORRUPT_OR_INCOMPLETE_MEDIA: RepairFailureCategory.PROVIDER_ISSUE,
    Code.IMPOSSIBLE_DURATION_OR_MOTION: RepairFailureCategory.IMPOSSIBLE_SHOT,
}

#: Most decisive first. Two diagnostics of the same severity are ordered here,
#: so the primary code never depends on dictionary or provider ordering.
DIAGNOSTIC_PRIORITY: tuple[RepairDiagnosticCode, ...] = (
    Code.CORRUPT_OR_INCOMPLETE_MEDIA,
    Code.UNSUPPORTED_PROVIDER_CAPABILITY,
    Code.PROVIDER_SAFETY_REJECTION,
    Code.PROVIDER_TIMEOUT_OR_SERVICE_FAILURE,
    Code.REFERENCE_CONFLICT,
    Code.IMPOSSIBLE_DURATION_OR_MOTION,
    Code.WRONG_CHARACTER_IDENTITY,
    Code.WRONG_CHARACTER_COUNT,
    Code.WRONG_LOCATION,
    Code.WRONG_WARDROBE_OR_STATE,
    Code.MISSING_OR_INCORRECT_ACTION,
    Code.CONTINUITY_FAILURE,
    Code.ANATOMY_OR_ARTIFACT_FAILURE,
    Code.COMPOSITION_FAILURE,
    Code.WEAK_MOTION,
    Code.STYLE_MISMATCH,
    Code.PROMPT_OVERCONSTRAINT,
    Code.PROMPT_AMBIGUITY,
)

#: A hard failure in one of these is categorical, not a near miss, so it earns a
#: structural repair however high the weighted score happens to be.
STRUCTURAL_HARD_FAILURES: frozenset[RepairDiagnosticCode] = frozenset(
    {
        Code.WRONG_CHARACTER_IDENTITY,
        Code.WRONG_CHARACTER_COUNT,
        Code.WRONG_LOCATION,
        Code.PROMPT_OVERCONSTRAINT,
        Code.COMPOSITION_FAILURE,
    }
)

#: A failure a paid generation cannot fix. Routing one of these to a provider
#: would spend money on a problem that lives in our own pipeline or in an
#: operation we already paid for.
DETERMINISTIC_ONLY: frozenset[RepairDiagnosticCode] = frozenset(
    {Code.CORRUPT_OR_INCOMPLETE_MEDIA, Code.UNSUPPORTED_PROVIDER_CAPABILITY}
)

#: Finding codes T20 emits when the approved reference itself is the problem,
#: rather than the generation failing to follow a valid one.
REFERENCE_INTEGRITY_FINDING_CODES: frozenset[str] = frozenset(
    {
        "reference_conflict",
        "reference_mismatch",
        "incompatible_reference",
        "missing_reference",
        "stale_reference",
        "reference_version_conflict",
    }
)


class ReferenceIntegrity(StrEnum):
    """What the repository says about the approved T19 references themselves."""

    VALID = "valid"
    MISSING = "missing"
    STALE = "stale"
    INCOMPATIBLE = "incompatible"
    CONFLICTING = "conflicting"


class TechnicalSignal(StrEnum):
    """A non-QA failure raised by the provider boundary or media processing."""

    PROVIDER_TIMEOUT = "provider_timeout"
    PROVIDER_SERVICE_FAILURE = "provider_service_failure"
    PROVIDER_SAFETY_REJECTION = "provider_safety_rejection"
    UNSUPPORTED_CAPABILITY = "unsupported_capability"
    CORRUPT_DOWNLOAD = "corrupt_download"
    IMPOSSIBLE_DURATION = "impossible_duration"


TECHNICAL_DIAGNOSTICS: dict[TechnicalSignal, RepairDiagnosticCode] = {
    TechnicalSignal.PROVIDER_TIMEOUT: Code.PROVIDER_TIMEOUT_OR_SERVICE_FAILURE,
    TechnicalSignal.PROVIDER_SERVICE_FAILURE: Code.PROVIDER_TIMEOUT_OR_SERVICE_FAILURE,
    TechnicalSignal.PROVIDER_SAFETY_REJECTION: Code.PROVIDER_SAFETY_REJECTION,
    TechnicalSignal.UNSUPPORTED_CAPABILITY: Code.UNSUPPORTED_PROVIDER_CAPABILITY,
    TechnicalSignal.CORRUPT_DOWNLOAD: Code.CORRUPT_OR_INCOMPLETE_MEDIA,
    TechnicalSignal.IMPOSSIBLE_DURATION: Code.IMPOSSIBLE_DURATION_OR_MOTION,
}


@dataclass(frozen=True, slots=True)
class ClassificationContext:
    """Everything outside the T20 result the classifier is allowed to consider."""

    reference_integrity: ReferenceIntegrity = ReferenceIntegrity.VALID
    technical_signals: tuple[TechnicalSignal, ...] = ()
    technical_messages: dict[TechnicalSignal, str] = field(default_factory=dict)


class RepairClassificationError(ValueError):
    """The persisted T20 result cannot be classified, so nothing is spent."""


def classify(
    result: VisualQAResult,
    *,
    context: ClassificationContext | None = None,
) -> RepairClassification:
    """Classify one failed T20 result into exactly one bounded category."""
    if result.outcome is VisualQAOutcome.PASS:
        raise RepairClassificationError("a passing T20 result has nothing to repair")
    resolved = context or ClassificationContext()
    diagnostics = _diagnostics(result, resolved)
    if not diagnostics:
        raise RepairClassificationError(
            "a failed T20 result carried no repair code, so no bounded repair exists"
        )
    primary = _primary(diagnostics)
    category = DIAGNOSTIC_CATEGORIES[primary.code]
    severity = _severity(result, primary, category)
    upstream = category is RepairFailureCategory.REFERENCE_ISSUE
    return RepairClassification(
        classifier_version=CLASSIFIER_VERSION,
        source_qa_result_id=result.qa_run_id,
        shot_id=result.target.storyboard_shot_id,
        target_type=result.target.target_type.value,
        category=category,
        severity=severity,
        primary_code=primary.code,
        diagnostics=diagnostics[:32],
        hard_failure=result.hard_failure,
        qa_score=result.score.total,
        pass_threshold=result.score.pass_threshold,
        importance=result.target.importance.value,
        confidence=result.score.confidence,
        deterministic_only=primary.code in DETERMINISTIC_ONLY,
        requires_upstream_reference_correction=upstream,
        rationale=_rationale(result, primary, severity, category),
    )


def _diagnostics(result: VisualQAResult, context: ClassificationContext) -> list[RepairDiagnostic]:
    """One diagnostic per distinct cause, deduplicated and evidence-linked."""
    collected: dict[RepairDiagnosticCode, RepairDiagnostic] = {}
    for signal in context.technical_signals:
        code = TECHNICAL_DIAGNOSTICS[signal]
        collected[code] = RepairDiagnostic(
            code=code,
            severity="hard_failure",
            repair_codes=_technical_repair_codes(code, result),
            dimension="provider_boundary",
            confidence=1.0,
            summary=context.technical_messages.get(signal, signal.value)[:500],
        )
    if context.reference_integrity is not ReferenceIntegrity.VALID:
        collected[Code.REFERENCE_CONFLICT] = _reference_diagnostic(result, context)
    for finding in _findings(result):
        finding_code = _finding_code(finding, context)
        if finding_code is None:
            continue
        candidate = _from_finding(finding, finding_code)
        existing = collected.get(finding_code)
        if existing is None or _outranks(candidate, existing):
            collected[finding_code] = candidate
    for repair_code in result.repair_codes:
        mapped_code = REPAIR_CODE_DIAGNOSTICS.get(repair_code)
        if mapped_code is None:
            raise RepairClassificationError(
                f"unmapped T20 repair code {repair_code.value!r}; the classifier and the "
                "T20 repair-code taxonomy have drifted"
            )
        if mapped_code not in collected:
            collected[mapped_code] = RepairDiagnostic(
                code=mapped_code,
                severity="hard_failure" if result.hard_failure else "warning",
                repair_codes=[repair_code],
                dimension="run",
                confidence=result.score.confidence,
                summary=f"T20 recorded repair code {repair_code.value}",
            )
    return [collected[code] for code in DIAGNOSTIC_PRIORITY if code in collected]


def _reference_diagnostic(
    result: VisualQAResult, context: ClassificationContext
) -> RepairDiagnostic:
    """Preserve the evidence explaining why an approved reference is invalid.

    T21 never fabricates a replacement reference and never pays to regenerate
    against one it already knows is wrong, so this diagnostic exists purely to
    carry the conflict upstream.
    """
    evidence = [
        finding
        for finding in _findings(result)
        if finding.code in REFERENCE_INTEGRITY_FINDING_CODES
        or any(item.compared_reference_asset_id is not None for item in finding.evidence)
    ]
    summary = f"the approved T19 reference is {context.reference_integrity.value}; " + (
        evidence[0].summary if evidence else "no generation can satisfy it as bound"
    )
    return RepairDiagnostic(
        code=Code.REFERENCE_CONFLICT,
        severity="hard_failure",
        repair_codes=list(result.repair_codes[:8]) or [VisualQARepairCode.HUMAN_REVIEW_REQUIRED],
        source_finding_ids=[item.finding_id for item in evidence][:16],
        dimension="reference_binding",
        confidence=1.0,
        evidence_timestamp_us=_timestamp(evidence),
        bounding_box=next(
            (
                item.bounding_box
                for finding in evidence
                for item in finding.evidence
                if item.bounding_box is not None
            ),
            None,
        ),
        summary=summary[:500],
    )


def _findings(result: VisualQAResult) -> list[VisualQAFinding]:
    return [
        finding
        for dimension in result.score.dimensions
        for finding in dimension.findings
        if finding.severity != "info"
    ]


def _finding_code(
    finding: VisualQAFinding, context: ClassificationContext
) -> RepairDiagnosticCode | None:
    if (
        finding.code in REFERENCE_INTEGRITY_FINDING_CODES
        and context.reference_integrity is not ReferenceIntegrity.VALID
    ):
        return Code.REFERENCE_CONFLICT
    for repair_code in finding.repair_codes:
        mapped = REPAIR_CODE_DIAGNOSTICS.get(repair_code)
        if mapped is not None:
            return mapped
    return _DIMENSION_FALLBACK.get(finding.dimension)


#: A finding that carries no repair code still names a rubric dimension, and the
#: dimension alone is enough to place it. It is never enough to *decide* a
#: category on its own, because a dimension has no severity of its own.
_DIMENSION_FALLBACK: dict[VisualQADimension, RepairDiagnosticCode] = {
    VisualQADimension.CHARACTER_IDENTITY: Code.WRONG_CHARACTER_IDENTITY,
    VisualQADimension.CHARACTER_COUNT: Code.WRONG_CHARACTER_COUNT,
    VisualQADimension.LOCATION: Code.WRONG_LOCATION,
    VisualQADimension.WARDROBE_AND_STATE: Code.WRONG_WARDROBE_OR_STATE,
    VisualQADimension.ACTION_AND_MOTION: Code.MISSING_OR_INCORRECT_ACTION,
    VisualQADimension.COMPOSITION: Code.COMPOSITION_FAILURE,
    VisualQADimension.ANATOMY_AND_ARTIFACTS: Code.ANATOMY_OR_ARTIFACT_FAILURE,
    VisualQADimension.CONTINUITY_AND_STYLE: Code.CONTINUITY_FAILURE,
}


def _from_finding(finding: VisualQAFinding, code: RepairDiagnosticCode) -> RepairDiagnostic:
    located = [item for item in finding.evidence if item.source_relative_timestamp_us is not None]
    return RepairDiagnostic(
        code=code,
        severity="hard_failure" if finding.severity == "hard_failure" else "warning",
        repair_codes=list(finding.repair_codes[:8])
        or (
            [VisualQARepairCode.HUMAN_REVIEW_REQUIRED] if finding.severity == "hard_failure" else []
        ),
        source_finding_ids=[finding.finding_id],
        dimension=finding.dimension.value,
        confidence=finding.confidence,
        evidence_timestamp_us=(located[0].source_relative_timestamp_us if located else None),
        bounding_box=next(
            (item.bounding_box for item in finding.evidence if item.bounding_box is not None),
            None,
        ),
        summary=finding.summary[:500],
    )


def _outranks(candidate: RepairDiagnostic, existing: RepairDiagnostic) -> bool:
    """Prefer a hard failure, then higher confidence, then a stable tie-break."""
    order = ("warning", "hard_failure")
    if order.index(candidate.severity) != order.index(existing.severity):
        return order.index(candidate.severity) > order.index(existing.severity)
    if candidate.confidence != existing.confidence:
        return candidate.confidence > existing.confidence
    return candidate.summary < existing.summary


def _primary(diagnostics: Sequence[RepairDiagnostic]) -> RepairDiagnostic:
    """The single most decisive diagnostic: hard failures first, then priority."""
    hard = [item for item in diagnostics if item.severity == "hard_failure"]
    ranked = hard or list(diagnostics)
    return min(ranked, key=lambda item: DIAGNOSTIC_PRIORITY.index(item.code))


def _severity(
    result: VisualQAResult,
    primary: RepairDiagnostic,
    category: RepairFailureCategory,
) -> RepairSeverity:
    if category is RepairFailureCategory.IMPOSSIBLE_SHOT:
        return RepairSeverity.UNRECOVERABLE
    if result.score.total < TARGETED_REPAIR_FLOOR:
        return RepairSeverity.STRUCTURAL
    if result.hard_failure and primary.code in STRUCTURAL_HARD_FAILURES:
        return RepairSeverity.STRUCTURAL
    return RepairSeverity.TARGETED


def _technical_repair_codes(
    code: RepairDiagnosticCode, result: VisualQAResult
) -> list[VisualQARepairCode]:
    if result.repair_codes:
        return list(result.repair_codes[:8])
    mapped = {
        Code.CORRUPT_OR_INCOMPLETE_MEDIA: VisualQARepairCode.DECODE_FAILURE,
        Code.IMPOSSIBLE_DURATION_OR_MOTION: VisualQARepairCode.DURATION_MISMATCH,
    }
    return [mapped.get(code, VisualQARepairCode.HUMAN_REVIEW_REQUIRED)]


def _timestamp(findings: Sequence[VisualQAFinding]) -> int | None:
    for finding in findings:
        for item in finding.evidence:
            if item.source_relative_timestamp_us is not None:
                return item.source_relative_timestamp_us
    return None


def _rationale(
    result: VisualQAResult,
    primary: RepairDiagnostic,
    severity: RepairSeverity,
    category: RepairFailureCategory,
) -> str:
    band = (
        "below the structural floor"
        if result.score.total < TARGETED_REPAIR_FLOOR
        else "within the targeted-repair band"
    )
    hard = "with a hard failure" if result.hard_failure else "with no hard failure"
    return (
        f"T20 scored {result.score.total:.2f} against a {result.score.pass_threshold:.0f} "
        f"{result.target.importance.value} threshold ({band}) {hard}; the decisive "
        f"diagnostic is {primary.code.value}, classified as {category.value} needing a "
        f"{severity.value} repair"
    )[:500]


__all__ = [
    "CLASSIFIER_VERSION",
    "DETERMINISTIC_ONLY",
    "DIAGNOSTIC_CATEGORIES",
    "DIAGNOSTIC_PRIORITY",
    "REFERENCE_INTEGRITY_FINDING_CODES",
    "REPAIR_CODE_DIAGNOSTICS",
    "STRUCTURAL_HARD_FAILURES",
    "TARGETED_REPAIR_FLOOR",
    "TECHNICAL_DIAGNOSTICS",
    "ClassificationContext",
    "ReferenceIntegrity",
    "RepairClassificationError",
    "TechnicalSignal",
    "classify",
]
