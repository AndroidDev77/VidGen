"""The human override of a soft visual-QA failure.

Scoring decides whether a shot failed; this decides what a person may do about
it afterwards. A ``REVIEW`` is an ambiguity the pipeline asked a human to
settle, and a ``FAIL`` that is not a hard failure is a verdict a human may
overrule - recorded distinctly, so the audit trail says which of the two
happened. A hard failure is a measured fact and stays outside both.

Which codes can produce a soft failure in the first place is the scoring
gate's business, covered in ``test_visual_qa_t20.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from services.qa.human_review import VisualQAHumanReviewService
from services.qa.rubric import RUBRIC, THRESHOLDS
from tests.review_fixtures import build_project_graph
from vidgen.contracts.visual_qa import VISUAL_QA_FORCE_APPROVED, VisualQATargetType
from vidgen.db.base import Base
from vidgen.db.visual_qa_models import VisualQARun
from vidgen.db.visual_qa_repository import VisualQARepository
from vidgen.review.errors import ReviewError


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
