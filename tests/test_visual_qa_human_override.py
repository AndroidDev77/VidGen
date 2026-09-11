"""Warn-only visual-QA codes, and the human override of a soft failure.

Two rules are asserted together here because they are two halves of one
change: a shot that fails only on judgement should reach a person rather than
the failure pile, and a person should be able to overrule that judgement. A
hard failure - a decode failure, a black clip - stays outside both.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from services.qa.human_review import VisualQAHumanReviewService
from services.qa.rubric import RUBRIC, THRESHOLDS
from services.qa.scoring import build_dimension_results, decide, recompute
from tests.review_fixtures import build_project_graph
from vidgen.contracts.visual_qa import (
    DEFAULT_VISUAL_QA_WARN_ONLY_CODES,
    VISUAL_QA_FORCE_APPROVED,
    VISUAL_QA_WARN_ONLY_ELIGIBLE_CODES,
    VisualQAAttemptType,
    VisualQADeterministicReport,
    VisualQADimension,
    VisualQAOutcome,
    VisualQAProviderDimensionScore,
    VisualQAProviderResult,
    VisualQARepairCode,
    VisualQASample,
    VisualQASampleType,
    VisualQAShotImportance,
    VisualQATargetType,
    VisualQAThresholds,
)
from vidgen.db.base import Base
from vidgen.db.visual_qa_models import VisualQARun
from vidgen.db.visual_qa_repository import VisualQARepository
from vidgen.review.errors import ReviewError

HASH = "a" * 64


# --- scoring inputs ----------------------------------------------------------
def sample(sequence: int) -> VisualQASample:
    timestamp = sequence * 1_000_000
    return VisualQASample.model_validate(
        {
            "sample_id": UUID(int=sequence + 1),
            "sequence": sequence,
            "sample_type": VisualQASampleType.COVERAGE,
            "requested_timestamp_us": timestamp,
            "actual_timestamp_us": timestamp,
            "shot_relative_timestamp_us": timestamp,
            "frame_asset_id": UUID(int=1000 + sequence),
            "frame_sha256": HASH,
            "source_asset_id": UUID(int=99),
            "selection_reason": "coverage",
            "contact_sheet_position": sequence,
        }
    )


def provider_result(
    *,
    scores: dict[VisualQADimension, float] | None = None,
    findings: list[dict[str, object]] | None = None,
) -> VisualQAProviderResult:
    return VisualQAProviderResult(
        qa_attempt_identity=HASH,
        attempt_type=VisualQAAttemptType.FIRST_PASS,
        dimension_scores=[
            VisualQAProviderDimensionScore(
                dimension=dimension,
                raw_score=(scores or {}).get(dimension, 95.0),
                confidence=0.9,
                applicable=True,
            )
            for dimension in VisualQADimension
        ],
        findings=[
            {
                "dimension": item["dimension"],
                "severity": item.get("severity", "warning"),
                "code": item.get("code", "issue"),
                "summary": item.get("summary", "an issue"),
                "repair_codes": item.get("repair_codes", []),
                "confidence": 0.9,
                "sample_ids": [UUID(int=1)],
            }
            for item in (findings or [])
        ],  # type: ignore[arg-type]
        overall_confidence=0.9,
        provider="fake",
        model="fake-visual-qa/1",
    )


def empty_report() -> VisualQADeterministicReport:
    return VisualQADeterministicReport.model_validate(
        {
            "check_version": "visual-qa-deterministic/1.0",
            "target_type": VisualQATargetType.VIDEO,
            "usable": True,
            "metrics": [],
        }
    )


def outcome_for(
    result: VisualQAProviderResult, *, thresholds: VisualQAThresholds = THRESHOLDS
) -> object:
    dimensions = build_dimension_results(
        result,
        empty_report(),
        rubric=RUBRIC,
        samples=[sample(0), sample(1)],
        source_asset_id=UUID(int=99),
    )
    score = recompute(
        dimensions,
        rubric=RUBRIC,
        thresholds=thresholds,
        importance=VisualQAShotImportance.NORMAL,
    )
    return decide(score, empty_report(), result, thresholds=thresholds)


# --- the warn-only set itself ------------------------------------------------
def test_a_hard_failure_measurement_can_never_be_made_warn_only() -> None:
    """Demoting a measurement would erase it; only judgements are demotable."""
    for code in (
        VisualQARepairCode.DECODE_FAILURE,
        VisualQARepairCode.BLACK_VIDEO,
        VisualQARepairCode.EXCESSIVE_FREEZE,
        VisualQARepairCode.EXCESSIVE_FLICKER,
        VisualQARepairCode.DURATION_MISMATCH,
    ):
        assert code.value not in VISUAL_QA_WARN_ONLY_ELIGIBLE_CODES
        with pytest.raises(ValueError):
            VisualQAThresholds(threshold_version="t", warn_only_codes=[code.value])


def test_the_deployment_default_names_the_four_false_positive_codes() -> None:
    assert set(THRESHOLDS.warn_only_codes) == set(DEFAULT_VISUAL_QA_WARN_ONLY_CODES)
    assert set(THRESHOLDS.warn_only_codes) == {
        "AMBIGUOUS_VISUAL_EVIDENCE",
        "INSUFFICIENT_MOTION",
        "PROMPT_TOO_COMPLEX",
        "TOO_MANY_REFERENCES",
    }


# --- scoring -----------------------------------------------------------------
def test_a_failure_whose_only_repair_code_is_warn_only_becomes_a_review() -> None:
    """The shot still failed on score; who decides changes, not what was measured."""
    result = provider_result(
        scores=dict.fromkeys(VisualQADimension, 80.0),
        findings=[
            {
                "dimension": VisualQADimension.COMPOSITION,
                "code": "too_much_going_on",
                "repair_codes": [VisualQARepairCode.PROMPT_TOO_COMPLEX],
            }
        ],
    )
    scored = outcome_for(result)
    assert scored.outcome is VisualQAOutcome.REVIEW  # type: ignore[attr-defined]
    assert scored.hard_failure is False  # type: ignore[attr-defined]
    # The demoted code is still carried: the repair consumer and the reviewer
    # both see exactly what the automated pass objected to.
    assert VisualQARepairCode.PROMPT_TOO_COMPLEX in scored.repair_codes  # type: ignore[attr-defined]
    assert scored.review_reasons  # type: ignore[attr-defined]


def test_the_same_failure_stays_a_failure_when_the_code_is_not_warn_only() -> None:
    result = provider_result(
        scores=dict.fromkeys(VisualQADimension, 80.0),
        findings=[
            {
                "dimension": VisualQADimension.CHARACTER_IDENTITY,
                "code": "not_maya",
                "repair_codes": [VisualQARepairCode.WRONG_CHARACTER_IDENTITY],
            }
        ],
    )
    scored = outcome_for(result)
    assert scored.outcome is VisualQAOutcome.FAIL  # type: ignore[attr-defined]


def test_a_score_below_the_repair_floor_stays_a_failure() -> None:
    """Warn-only softens a judgement, not a structurally bad shot.

    Below the targeted-repair floor the routing adds the failing dimension's own
    structural repair code, which is not warn-only and is not a false positive:
    the shot is wrong, not merely hard to judge.
    """
    result = provider_result(
        scores=dict.fromkeys(VisualQADimension, 40.0),
        findings=[
            {
                "dimension": VisualQADimension.COMPOSITION,
                "code": "too_much_going_on",
                "repair_codes": [VisualQARepairCode.PROMPT_TOO_COMPLEX],
            }
        ],
    )
    scored = outcome_for(result)
    assert scored.outcome is VisualQAOutcome.FAIL  # type: ignore[attr-defined]
    assert scored.hard_failure is False  # type: ignore[attr-defined]


def test_a_below_floor_failure_is_a_review_when_every_code_it_routes_is_warn_only() -> None:
    """The rule is about the codes, not about which branch produced them."""
    thresholds = VisualQAThresholds(
        threshold_version="t",
        warn_only_codes=[
            "COMPOSITION_MISMATCH",
            "PROMPT_TOO_COMPLEX",
            "TOO_MANY_CHARACTERS",
            "WRONG_CHARACTER_COUNT",
        ],
    )
    scores = dict.fromkeys(VisualQADimension, 70.0)
    scores[VisualQADimension.COMPOSITION] = 0.0
    result = provider_result(
        scores=scores,
        findings=[
            {
                "dimension": VisualQADimension.COMPOSITION,
                "code": "too_much_going_on",
                "repair_codes": [VisualQARepairCode.PROMPT_TOO_COMPLEX],
            }
        ],
    )
    scored = outcome_for(result, thresholds=thresholds)
    assert scored.outcome is VisualQAOutcome.REVIEW  # type: ignore[attr-defined]


def test_an_empty_warn_only_set_restores_the_previous_behaviour() -> None:
    thresholds = VisualQAThresholds(threshold_version="t", warn_only_codes=[])
    result = provider_result(
        scores=dict.fromkeys(VisualQADimension, 80.0),
        findings=[
            {
                "dimension": VisualQADimension.COMPOSITION,
                "code": "too_much_going_on",
                "repair_codes": [VisualQARepairCode.PROMPT_TOO_COMPLEX],
            }
        ],
    )
    scored = outcome_for(result, thresholds=thresholds)
    assert scored.outcome is VisualQAOutcome.FAIL  # type: ignore[attr-defined]


def test_a_demoted_dimension_hard_failure_never_silently_passes() -> None:
    """A high score plus a demoted blocker is a decision, not a pass."""
    thresholds = VisualQAThresholds(
        threshold_version="t", warn_only_codes=["WRONG_CHARACTER_IDENTITY"]
    )
    result = provider_result(
        scores=dict.fromkeys(VisualQADimension, 99.0),
        findings=[
            {
                "dimension": VisualQADimension.CHARACTER_IDENTITY,
                "severity": "hard_failure",
                "code": "not_maya",
                "repair_codes": [VisualQARepairCode.WRONG_CHARACTER_IDENTITY],
            }
        ],
    )
    scored = outcome_for(result, thresholds=thresholds)
    assert scored.outcome is VisualQAOutcome.REVIEW  # type: ignore[attr-defined]
    assert scored.hard_failure is False  # type: ignore[attr-defined]
    assert "WRONG_CHARACTER_IDENTITY" in scored.warning_codes  # type: ignore[attr-defined]


# --- human override ----------------------------------------------------------
@pytest.fixture
def qa_session(tmp_path: Path) -> Session:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'override.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def qa_run(session: Session, blob_root: Path, *, outcome: str, hard_failure: bool) -> VisualQARun:
    graph = build_project_graph(session, blob_root=blob_root)
    run = VisualQARun(
        id=uuid4(),
        project_id=graph.project_id,
        storyboard_run_id=graph.storyboard_run_id,
        shot_id=graph.shot_ids[0],
        shot_workflow_identity="a" * 64,
        target_type=VisualQATargetType.VIDEO.value,
        target_asset_id=graph.keyframe_asset_ids[0],
        target_asset_sha256="b" * 64,
        qa_identity="c" * 64,
        input_hash="d" * 64,
        idempotency_key=f"override:{uuid4()}",
        status="visual_qa_complete",
        importance="normal",
        rubric_version=RUBRIC.rubric_version,
        sampling_version="visual-qa-sampler/1.0",
        threshold_version=THRESHOLDS.threshold_version,
        deterministic_version="visual-qa-deterministic/1.0",
        pipeline_version="visual-qa/1.0.0",
        deterministic_report={},
        final_outcome=outcome,
        final_score=70.0,
        pass_threshold=85.0,
        hard_failure=hard_failure,
        repair_recommendation="PROMPT_SIMPLIFICATION",
        repair_codes=["PROMPT_TOO_COMPLEX"],
        warning_codes=[],
        cost_microusd=0,
        completed_at=datetime.now(UTC),
    )
    session.add(run)
    session.flush()
    return run


def test_a_soft_failure_can_be_force_approved_and_the_gate_opens(
    qa_session: Session, tmp_path: Path
) -> None:
    run = qa_run(qa_session, tmp_path / "blobs", outcome="FAIL", hard_failure=False)
    repository = VisualQARepository(qa_session)
    assert repository.gate(run.shot_id, VisualQATargetType.VIDEO) == (False, "visual_qa_failed")

    outcome = VisualQAHumanReviewService(qa_session, "owner-a").decide(
        run,
        decision="approved",
        reason="the framing is fine; the score is wrong",
        row_version=1,
        idempotency_key="force-1",
    )
    # The override is recorded under its own name, never as a plain approval.
    assert outcome.decision == VISUAL_QA_FORCE_APPROVED
    assert outcome.resulting_gate == "visual_qa_human_force_approved"
    assert repository.gate(run.shot_id, VisualQATargetType.VIDEO)[0] is True
    # The automated result is untouched: the gate consults the review instead.
    assert run.final_outcome == "FAIL"


def test_a_hard_failure_is_still_refused(qa_session: Session, tmp_path: Path) -> None:
    run = qa_run(qa_session, tmp_path / "blobs", outcome="FAIL", hard_failure=True)
    with pytest.raises(ReviewError) as raised:
        VisualQAHumanReviewService(qa_session, "owner-a").decide(
            run,
            decision="approved",
            reason="looks fine to me",
            row_version=1,
            idempotency_key="force-2",
        )
    assert "hard failure" in raised.value.error.summary
    assert VisualQARepository(qa_session).gate(run.shot_id, VisualQATargetType.VIDEO)[0] is False


def test_rejecting_a_soft_failure_keeps_the_plain_decision(
    qa_session: Session, tmp_path: Path
) -> None:
    """Only an approval is an override; a rejection agrees with the pipeline."""
    run = qa_run(qa_session, tmp_path / "blobs", outcome="FAIL", hard_failure=False)
    outcome = VisualQAHumanReviewService(qa_session, "owner-a").decide(
        run,
        decision="rejected",
        reason="agreed, regenerate it",
        row_version=1,
        idempotency_key="reject-1",
    )
    assert outcome.decision == "rejected"
    assert VisualQARepository(qa_session).gate(run.shot_id, VisualQATargetType.VIDEO)[0] is False


def test_approving_an_ambiguous_review_is_still_a_plain_approval(
    qa_session: Session, tmp_path: Path
) -> None:
    run = qa_run(qa_session, tmp_path / "blobs", outcome="REVIEW", hard_failure=False)
    outcome = VisualQAHumanReviewService(qa_session, "owner-a").decide(
        run,
        decision="approved",
        reason="that is Maya",
        row_version=1,
        idempotency_key="review-1",
    )
    assert outcome.decision == "approved"
    assert outcome.resulting_gate == "visual_qa_human_approved"


def test_a_passing_run_has_nothing_for_a_human_to_decide(
    qa_session: Session, tmp_path: Path
) -> None:
    run = qa_run(qa_session, tmp_path / "blobs", outcome="PASS", hard_failure=False)
    with pytest.raises(ReviewError):
        VisualQAHumanReviewService(qa_session, "owner-a").decide(
            run,
            decision="approved",
            reason="already passing",
            row_version=1,
            idempotency_key="pass-1",
        )
