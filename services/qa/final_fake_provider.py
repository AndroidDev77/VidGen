"""A deterministic fake final-editorial provider.

The fake makes the whole T22 pipeline runnable - locally, in tests and in CI -
without a paid credential and without a network call. It is deterministic by
construction: the same request and the same configured defect profile always
produce the same dimension scores and findings, so gate recomputation,
adjudication and completion enforcement can be asserted exactly.

With no profile the fake returns a clean, passing editorial evaluation of the
assembled recap. A controlled-defect fixture configures a
:class:`FakeEditorialDefect` to produce a specific blocking or borderline
finding, including the deliberately high dimension scores that must *not* be
allowed to conceal it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from services.qa.final_editorial_provider import FinalEditorialCall
from services.qa.final_rubric import EDITORIAL_DIMENSIONS
from vidgen.contracts.final_editorial import (
    FinalEditorialCategory,
    FinalEditorialDimension,
    FinalEditorialProviderFinding,
    FinalEditorialProviderResult,
    FinalFindingSeverity,
    FinalIssueCode,
    FinalRemediationTarget,
)

CLEAN_SCORE = 94.0
CLEAN_CONFIDENCE = 0.95


@dataclass(frozen=True, slots=True)
class FakeEditorialFinding:
    """One controlled editorial finding, anchored to the render's own timeline."""

    category: FinalEditorialCategory
    issue_code: FinalIssueCode
    severity: FinalFindingSeverity
    summary: str
    start_us: int = 0
    end_us: int = 0
    confidence: float = 0.9
    #: Which sampled frame supports the finding, by position in the request.
    sample_index: int | None = 0
    #: Which selected shot the finding accuses, by position in the shot map.
    shot_index: int | None = None
    caption_cue_sequences: tuple[int, ...] = ()
    expected_behavior: str = ""
    observed_behavior: str = ""
    remediation: FinalRemediationTarget = FinalRemediationTarget.NONE


@dataclass(frozen=True, slots=True)
class FakeEditorialDefect:
    """One controlled editorial profile for one render identity."""

    dimension_scores: Mapping[FinalEditorialCategory, float] = field(default_factory=dict)
    dimension_confidence: Mapping[FinalEditorialCategory, float] = field(default_factory=dict)
    inapplicable: frozenset[FinalEditorialCategory] = frozenset()
    findings: tuple[FakeEditorialFinding, ...] = ()
    overall_confidence: float = CLEAN_CONFIDENCE
    narrative_summary: str = "the assembled recap tells the approved story"
    #: The profile used only when this run is escalated to adjudication.
    adjudication: FakeEditorialDefect | None = None


class FakeFinalEditorialProvider:
    """A deterministic editorial evaluator keyed by render identity."""

    def __init__(
        self,
        defects: Mapping[str, FakeEditorialDefect] | None = None,
        *,
        name: str = "fake",
        model: str = "fake-final-editorial-1",
        adjudicator: bool = False,
    ) -> None:
        self._defects = dict(defects or {})
        self._name = name
        self._model = model
        self._adjudicator = adjudicator
        self.calls: list[FinalEditorialCall] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def model(self) -> str:
        return self._model

    async def evaluate(self, call: FinalEditorialCall) -> FinalEditorialProviderResult:
        self.calls.append(call)
        request = call.request
        defect = self._defects.get(request.render_identity, FakeEditorialDefect())
        if self._adjudicator and defect.adjudication is not None:
            defect = defect.adjudication
        return FinalEditorialProviderResult(
            attempt_identity=request.attempt_identity,
            attempt_type=request.attempt_type,
            provider=self._name,
            model=self._model,
            provider_request_id=f"fake-{request.attempt_identity[:16]}",
            dimension_scores=[
                FinalEditorialDimension(
                    category=category,
                    applicable=category not in defect.inapplicable,
                    score=float(defect.dimension_scores.get(category, CLEAN_SCORE)),
                    confidence=float(defect.dimension_confidence.get(category, CLEAN_CONFIDENCE)),
                    summary="",
                )
                for category in EDITORIAL_DIMENSIONS
            ],
            findings=[self._finding(call, finding) for finding in defect.findings],
            overall_confidence=defect.overall_confidence,
            narrative_summary=defect.narrative_summary,
            usage={"input_tokens": 1200.0, "output_tokens": 400.0},
            redacted_metadata={"provider": self._name, "deterministic": "true"},
        )

    def _finding(
        self, call: FinalEditorialCall, finding: FakeEditorialFinding
    ) -> FinalEditorialProviderFinding:
        request = call.request
        samples = request.samples
        sample_ids = []
        if finding.sample_index is not None and 0 <= finding.sample_index < len(samples):
            evidence = samples[finding.sample_index]
            sample_ids = [evidence.sample_id] if evidence.sample_id is not None else []
        shot_ids = []
        if finding.shot_index is not None:
            shots = [item.shot_id for item in samples if item.shot_id is not None]
            ordered = list(dict.fromkeys(shots))
            if 0 <= finding.shot_index < len(ordered):
                shot_ids = [ordered[finding.shot_index]]
        end = finding.end_us or finding.start_us
        return FinalEditorialProviderFinding(
            category=finding.category,
            issue_code=finding.issue_code,
            proposed_severity=finding.severity,
            confidence=finding.confidence,
            summary=finding.summary,
            start_us=min(finding.start_us, request.timeline_duration_us),
            end_us=min(end, request.timeline_duration_us),
            shot_ids=shot_ids,
            sample_ids=sample_ids,
            caption_cue_sequences=list(finding.caption_cue_sequences),
            expected_behavior=finding.expected_behavior,
            observed_behavior=finding.observed_behavior,
            proposed_remediation=finding.remediation,
        )
