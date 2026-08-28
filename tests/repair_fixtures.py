"""Deterministic T21 fixtures: scripted QA verdicts over the real T20 pipeline.

T21 has to be exercised against QA verdicts that *change* between attempts: the
original clip fails, each repair fails, and the deterministic fallback finally
passes. The fake visual agent is keyed by shot, so these fixtures wrap it in a
scripted agent that returns one profile per evaluation instead.

Everything here is deterministic and credential-free. The T20 pipeline that runs
is the real one - real sampling, real deterministic media checks, real score
recomputation - only the visual agent's verdict is scripted.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from services.qa.fake_visual_agent import FakeDefect, FakeFinding, FakeVisualAgent
from services.qa.pipeline import VisualQAOptions, VisualQAPipeline
from services.qa.repair import Revalidator
from services.qa.rubric import RUBRIC, THRESHOLDS
from services.qa.visual_agent import VisualAgentCall, VisualQARole
from vidgen.contracts.visual_qa import (
    VisualQADeterministicReport,
    VisualQADimension,
    VisualQADimensionResult,
    VisualQAEvidence,
    VisualQAEvidenceType,
    VisualQAFinding,
    VisualQAOutcome,
    VisualQAProviderResult,
    VisualQARepairCode,
    VisualQARepairRecommendation,
    VisualQAResult,
    VisualQARoutingRecommendation,
    VisualQASample,
    VisualQASampleType,
    VisualQASamplingManifest,
    VisualQAScore,
    VisualQAShotImportance,
    VisualQATarget,
    VisualQATargetType,
)
from vidgen.storage.blob import BlobStore


def identity_resolver(_session: Session, _storyboard: object, _shot: object) -> str:
    """A stable T16 child-workflow identity for the fixture graph."""
    return "a" * 64


def failing_profile(
    *,
    score: float = 60.0,
    repair_code: VisualQARepairCode = VisualQARepairCode.WRONG_ACTION,
    dimension: VisualQADimension = VisualQADimension.ACTION_AND_MOTION,
    hard: bool = False,
    summary: str = "The mandatory beat is not visible in the clip.",
) -> FakeDefect:
    """One controlled failing verdict with a bounded repair code."""
    return FakeDefect(
        dimension_scores=dict.fromkeys(VisualQADimension, score),
        findings=(
            FakeFinding(
                dimension=dimension,
                severity="hard_failure" if hard else "warning",
                code=repair_code.value.lower(),
                summary=summary,
                repair_codes=(repair_code,),
                confidence=0.93,
                proposed_correction="State the beat explicitly and keep it on screen.",
            ),
        ),
        repair_codes=(repair_code,),
        proposed_hard_failure_codes=(repair_code.value,) if hard else (),
    )


def passing_profile(score: float = 96.0) -> FakeDefect:
    return FakeDefect(dimension_scores=dict.fromkeys(VisualQADimension, score))


class ScriptedVisualAgent(FakeVisualAgent):
    """A deterministic agent that returns one scripted profile per evaluation.

    The last profile repeats, so a script that ends in a passing verdict keeps
    passing and a script that ends in a failing one keeps failing - which is what
    makes "the policy stops after four attempts" observable rather than assumed.
    """

    def __init__(
        self,
        profiles: Sequence[FakeDefect],
        *,
        role: VisualQARole = VisualQARole.LUNA_FIRST_PASS,
        model: str = "fake-visual-qa/1",
    ) -> None:
        super().__init__({}, role=role, model=model)
        self._profiles = list(profiles)
        self.evaluations = 0
        self._leader: ScriptedVisualAgent | None = None

    def share_position_with(self, leader: ScriptedVisualAgent) -> None:
        """Follow another agent's script position instead of keeping its own."""
        self._leader = leader

    @property
    def position(self) -> int:
        return self._leader.position if self._leader is not None else self.evaluations

    def profile_for(self, shot_id: UUID) -> FakeDefect:
        del shot_id
        index = min(self.position, len(self._profiles) - 1)
        return self._profiles[index]

    async def evaluate(self, call: VisualAgentCall) -> VisualQAProviderResult:
        result = await super().evaluate(call)
        if call.first_pass is None:
            # Adjudication re-reads the same evaluation; only a first pass
            # advances the script.
            self.evaluations += 1
        return result


def scripted_revalidator(
    session: Session,
    blob_store: BlobStore,
    profiles: Sequence[FakeDefect],
    *,
    width: int = 320,
    height: int = 180,
    identity: Callable[..., str] | None = None,
) -> tuple[Revalidator, ScriptedVisualAgent]:
    """A T20 revalidator whose verdicts follow ``profiles`` in order."""
    agent = ScriptedVisualAgent(profiles)
    # T20 requires a bounded second opinion whenever a target's verdict changes
    # between runs, which is exactly what a repair does. The same scripted agent
    # adjudicates: adjudication calls carry the first pass, so they read the
    # current profile without advancing the script.
    adjudicator = ScriptedVisualAgent(
        profiles, role=VisualQARole.TERRA_ADJUDICATOR, model="fake-visual-qa-adjudicator/1"
    )
    adjudicator.share_position_with(agent)

    async def revalidate(
        *, project_id: UUID, shot_id: UUID, idempotency_key: str
    ) -> VisualQAResult:
        pipeline = VisualQAPipeline(
            session,
            blob_store,
            agent,
            adjudicator=adjudicator,
            shot_workflow_identity_resolver=identity or identity_resolver,
            options=VisualQAOptions(expected_width=width, expected_height=height),
        )
        return await pipeline.evaluate_shot(
            project_id=project_id,
            shot_id=shot_id,
            target_type=VisualQATargetType.VIDEO,
            idempotency_key=idempotency_key,
        )

    return revalidate, agent


__all__ = [
    "ScriptedVisualAgent",
    "failing_profile",
    "identity_resolver",
    "passing_profile",
    "qa_result",
    "scripted_revalidator",
]


def qa_result(
    *,
    score: float = 60.0,
    repair_codes: Sequence[VisualQARepairCode] = (VisualQARepairCode.WRONG_ACTION,),
    hard_failure_dimension: VisualQADimension | None = None,
    hard_failure_code: VisualQARepairCode | None = None,
    finding_dimension: VisualQADimension = VisualQADimension.ACTION_AND_MOTION,
    finding_code: str = "wrong_action",
    finding_repair_codes: Sequence[VisualQARepairCode] = (VisualQARepairCode.WRONG_ACTION,),
    importance: VisualQAShotImportance = VisualQAShotImportance.NORMAL,
    outcome: VisualQAOutcome = VisualQAOutcome.FAIL,
    compared_reference_asset_id: UUID | None = None,
    shot_id: UUID | None = None,
) -> VisualQAResult:
    """One in-memory T20 result, valid against every T20 contract validator.

    The classifier reads persisted T20 results, so these fixtures build the real
    contract rather than a loose stand-in: the score is a genuine weighted
    recomputation and a hard failure is structurally separate from the score.
    """
    identifier = shot_id or uuid4()
    sample_id = uuid4()
    source_asset_id = uuid4()
    evidence = VisualQAEvidence(
        evidence_id=uuid4(),
        evidence_type=VisualQAEvidenceType.SAMPLE_FRAME,
        sample_id=sample_id,
        source_asset_id=source_asset_id,
        source_relative_timestamp_us=500_000,
        shot_relative_timestamp_us=500_000,
        compared_reference_asset_id=compared_reference_asset_id,
        confidence=0.9,
        explanation="deterministic fixture evidence",
    )
    findings = [
        VisualQAFinding(
            finding_id=uuid4(),
            dimension=finding_dimension,
            severity="warning",
            code=finding_code,
            summary="The mandatory beat is not visible in the clip.",
            repair_codes=list(finding_repair_codes),
            confidence=0.9,
            evidence=[evidence],
        )
    ]
    if hard_failure_dimension is not None and hard_failure_code is not None:
        findings.append(
            VisualQAFinding(
                finding_id=uuid4(),
                dimension=hard_failure_dimension,
                severity="hard_failure",
                code=hard_failure_code.value.lower(),
                summary="A categorical failure the score can never override.",
                repair_codes=[hard_failure_code],
                confidence=0.97,
                evidence=[evidence],
            )
        )
    dimensions = [
        VisualQADimensionResult(
            dimension=item.dimension,
            applicable=True,
            raw_score=score,
            weight=item.weight,
            effective_weight=item.weight,
            weighted_contribution=score * item.weight / 100,
            confidence=0.9,
            findings=[finding for finding in findings if finding.dimension is item.dimension],
            hard_failure_codes=[
                finding.code
                for finding in findings
                if finding.dimension is item.dimension and finding.severity == "hard_failure"
            ],
            repair_codes=[
                code
                for finding in findings
                if finding.dimension is item.dimension
                for code in finding.repair_codes
            ],
            evaluator="fake",
            model="fake-visual-qa/1",
            rubric_version=RUBRIC.rubric_version,
        )
        for item in RUBRIC.dimensions
    ]
    hard = hard_failure_code is not None
    return VisualQAResult(
        qa_run_id=uuid4(),
        qa_identity="a" * 64,
        input_hash="b" * 64,
        target=VisualQATarget(
            project_id=uuid4(),
            storyboard_run_id=uuid4(),
            storyboard_shot_id=identifier,
            shot_sequence=0,
            target_type=VisualQATargetType.VIDEO,
            target_asset_id=uuid4(),
            target_asset_sha256="c" * 64,
            media_type="video/mp4",
            shot_workflow_identity="d" * 64,
            canonical_shot_hash="e" * 64,
            shot_reference_bundle_hash="f" * 64,
            importance=importance,
            usable_duration_us=3_000_000,
            requested_generation_duration_us=4_000_000,
        ),
        outcome=VisualQAOutcome.FAIL if hard else outcome,
        score=VisualQAScore(
            rubric_version=RUBRIC.rubric_version,
            threshold_version=THRESHOLDS.threshold_version,
            importance=importance,
            pass_threshold=THRESHOLDS.pass_score(importance),
            total=score,
            applied_weight_total=100,
            dimensions=dimensions,
            confidence=0.9,
        ),
        hard_failure=hard,
        hard_failure_codes=[hard_failure_code.value] if hard_failure_code else [],
        repair_codes=list(repair_codes),
        recommendation=VisualQARepairRecommendation(
            routing=VisualQARoutingRecommendation.TARGETED_REPAIR,
            repair_codes=list(repair_codes),
        ),
        deterministic_report=VisualQADeterministicReport(
            check_version="visual-qa-deterministic/1.0",
            target_type=VisualQATargetType.VIDEO,
            usable=True,
        ),
        sampling_manifest=VisualQASamplingManifest(
            sampling_version="visual-qa-sampler/1.0",
            target_type=VisualQATargetType.VIDEO,
            source_asset_id=source_asset_id,
            measured_duration_us=3_000_000,
            samples=[
                VisualQASample(
                    sample_id=sample_id,
                    sequence=0,
                    sample_type=VisualQASampleType.FIRST_FRAME,
                    requested_timestamp_us=0,
                    actual_timestamp_us=0,
                    shot_relative_timestamp_us=0,
                    frame_sha256="1" * 64,
                    source_asset_id=source_asset_id,
                    selection_reason="first decodable frame",
                )
            ],
        ),
        first_pass_provider="fake",
        first_pass_model="fake-visual-qa/1",
        pipeline_version="visual-qa/1.0.0",
        created_at=datetime.now(UTC),
    )
