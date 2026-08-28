"""A deterministic fake visual agent.

The fake makes the whole T20 pipeline runnable - locally, in tests and in CI -
without a paid credential and without a network call. It is deterministic by
construction: the same request and the same configured defect profile always
produce the same dimension scores, findings and repair codes, so score
recomputation, adjudication and gating can be asserted exactly.

Controlled-defect fixtures configure a :class:`FakeDefect` per shot. With no
profile the fake returns a clean, passing evaluation.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from uuid import UUID

from services.qa.visual_agent import VisualAgentCall, VisualQARole
from vidgen.contracts.visual_qa import (
    VisualQADimension,
    VisualQAProviderDimensionScore,
    VisualQAProviderFinding,
    VisualQAProviderResult,
    VisualQARepairCode,
)

CLEAN_SCORE = 96.0
CLEAN_CONFIDENCE = 0.95


@dataclass(frozen=True, slots=True)
class FakeFinding:
    dimension: VisualQADimension
    severity: str
    code: str
    summary: str
    repair_codes: tuple[VisualQARepairCode, ...] = ()
    confidence: float = 0.9
    proposed_correction: str = ""
    #: Which sampled frame supports the finding, by position in the manifest.
    sample_index: int = 0


@dataclass(frozen=True, slots=True)
class FakeDefect:
    """One controlled defect profile for one shot and target type."""

    dimension_scores: Mapping[VisualQADimension, float] = field(default_factory=dict)
    dimension_confidence: Mapping[VisualQADimension, float] = field(default_factory=dict)
    inapplicable: frozenset[VisualQADimension] = frozenset()
    findings: tuple[FakeFinding, ...] = ()
    repair_codes: tuple[VisualQARepairCode, ...] = ()
    warning_codes: tuple[str, ...] = ()
    proposed_hard_failure_codes: tuple[str, ...] = ()
    overall_confidence: float = CLEAN_CONFIDENCE
    #: An adjudicator profile used only when this shot is escalated.
    adjudication: FakeDefect | None = None


class FakeVisualAgent:
    """A provider-neutral, deterministic stand-in for a configured visual agent."""

    def __init__(
        self,
        defects: Mapping[UUID, FakeDefect] | None = None,
        *,
        role: VisualQARole = VisualQARole.LUNA_FIRST_PASS,
        model: str = "fake-visual-qa/1",
    ) -> None:
        self._defects = dict(defects or {})
        self._role = role
        self._model = model
        self.calls: list[VisualAgentCall] = []

    @property
    def name(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return self._model

    def profile_for(self, shot_id: UUID) -> FakeDefect:
        return self._defects.get(shot_id, FakeDefect())

    async def evaluate(self, call: VisualAgentCall) -> VisualQAProviderResult:
        self.calls.append(call)
        request = call.request
        profile = self.profile_for(request.storyboard_shot_id)
        if call.first_pass is not None and profile.adjudication is not None:
            profile = profile.adjudication
        scores = [
            VisualQAProviderDimensionScore(
                dimension=dimension,
                raw_score=float(profile.dimension_scores.get(dimension, CLEAN_SCORE)),
                confidence=float(profile.dimension_confidence.get(dimension, CLEAN_CONFIDENCE)),
                applicable=dimension not in profile.inapplicable,
                summary=f"deterministic fake evaluation of {dimension.value}",
            )
            for dimension in VisualQADimension
        ]
        findings = [
            VisualQAProviderFinding(
                dimension=item.dimension,
                severity=item.severity,  # type: ignore[arg-type]
                code=item.code,
                summary=item.summary,
                proposed_correction=item.proposed_correction,
                repair_codes=list(item.repair_codes),
                confidence=item.confidence,
                sample_ids=[
                    request.samples[min(item.sample_index, len(request.samples) - 1)].sample_id
                ],
            )
            for item in profile.findings
        ]
        return VisualQAProviderResult(
            qa_attempt_identity=request.qa_attempt_identity,
            attempt_type=request.attempt_type,
            dimension_scores=scores,
            findings=findings,
            proposed_hard_failure_codes=list(profile.proposed_hard_failure_codes),
            repair_codes=list(profile.repair_codes),
            warning_codes=list(profile.warning_codes),
            overall_confidence=profile.overall_confidence,
            provider=self.name,
            model=self._model,
            provider_request_id=f"fake-{request.qa_attempt_identity[:16]}",
            usage={"evaluated_frames": float(len(request.samples))},
            redacted_metadata={"role": self._role.value},
        )
