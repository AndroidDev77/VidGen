"""Restartability, lineage, cost and gating behaviour of the T20 pipeline."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

import vidgen.db  # noqa: F401
from services.qa.commands import (
    VisualQABlocked,
    VisualQACommandOptions,
    VisualQAReviewRequired,
    evaluate_shot_stage,
    run_visual_qa,
)
from services.qa.contracts import AuthoritativeInputSelector, VisualQALineageError
from services.qa.fake_visual_agent import FakeDefect, FakeFinding, FakeVisualAgent
from services.qa.human_review import VisualQAHumanReviewService
from services.qa.pipeline import VisualQAPipeline
from tests.visual_qa_fixtures import VisualQAFixture, build_visual_qa_project
from vidgen.contracts.visual_qa import (
    VisualQADimension,
    VisualQAFailureCode,
    VisualQAOutcome,
    VisualQARepairCode,
    VisualQATargetType,
)
from vidgen.db.base import Base
from vidgen.db.continuity_models import character_identity_versions
from vidgen.db.cost_models import CostLedgerEntry, ProjectBudget, ProviderAttempt
from vidgen.db.cost_repository import BudgetExceededError
from vidgen.db.image_generation_models import GeneratedKeyframeImage
from vidgen.db.models import Asset
from vidgen.db.visual_qa_models import VisualQAAttempt, VisualQARun, VisualQASampleRecord
from vidgen.db.visual_qa_repository import VisualQARepository
from vidgen.review.errors import ReviewError
from vidgen.storage.blob import FilesystemBlobStore


def resolver(_session: Session, _storyboard: object, _shot: object) -> str:
    return "a" * 64


@pytest.fixture
def graph(tmp_path: Path) -> Iterator[tuple[Session, FilesystemBlobStore, VisualQAFixture]]:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'pipeline.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    blob_root = tmp_path / "blobs"
    store = FilesystemBlobStore(blob_root, b"test-secret")
    with factory() as session:
        fixture = build_visual_qa_project(session, blob_root, tmp_path, shot_count=2)
        yield session, store, fixture


def run_one(
    session: Session,
    store: FilesystemBlobStore,
    fixture: VisualQAFixture,
    *,
    index: int = 0,
    target: VisualQATargetType = VisualQATargetType.VIDEO,
    defects: dict[UUID, FakeDefect] | None = None,
    key: str | None = None,
):
    outcome = asyncio.run(
        run_visual_qa(
            session,
            store,
            project_id=fixture.project_id,
            options=VisualQACommandOptions(
                provider="fake",
                fake_defects=defects or {},
                shot_id=fixture.shot_ids[index],
                targets=(target,),
                idempotency_key=key,
            ),
            identity_resolver=resolver,
        )
    )
    assert outcome.failures == (), outcome.failures
    return outcome.results[0]


# --- authoritative input selection -------------------------------------------
def test_authoritative_selection_binds_the_approved_reference_lineage(
    graph: tuple[Session, FilesystemBlobStore, VisualQAFixture],
) -> None:
    session, _, fixture = graph
    selector = AuthoritativeInputSelector(session, shot_workflow_identity_resolver=resolver)
    inputs = selector.select(fixture.project_id, fixture.shot_ids[0], VisualQATargetType.VIDEO)
    target = inputs.target()
    assert target.project_id == fixture.project_id
    assert target.character_identity_version_ids == [fixture.character_identity_version_id]
    assert target.location_identity_version_id == fixture.location_identity_version_id
    assert target.character_reference_asset_ids
    assert target.location_reference_asset_ids
    assert target.character_state_snapshot_hashes
    assert target.location_state_snapshot_hash is not None
    assert target.required_props == ["mug"]


def test_a_cross_project_shot_is_rejected(
    graph: tuple[Session, FilesystemBlobStore, VisualQAFixture], tmp_path: Path
) -> None:
    session, _, fixture = graph
    other = build_visual_qa_project(
        session,
        tmp_path / "blobs",
        tmp_path / "other",
        owner_subject="owner-b",
        name="Other episode",
        shot_count=1,
    )
    selector = AuthoritativeInputSelector(session, shot_workflow_identity_resolver=resolver)
    with pytest.raises(VisualQALineageError) as error:
        selector.select(fixture.project_id, other.shot_ids[0], VisualQATargetType.VIDEO)
    assert error.value.code is VisualQAFailureCode.SHOT_NOT_FOUND
    assert error.value.retryable is False


def test_a_stale_asset_hash_is_rejected(
    graph: tuple[Session, FilesystemBlobStore, VisualQAFixture],
) -> None:
    session, _, fixture = graph
    keyframe = session.query(GeneratedKeyframeImage).filter_by(shot_id=fixture.shot_ids[0]).one()
    keyframe.sha256 = "b" * 64
    session.flush()
    selector = AuthoritativeInputSelector(session, shot_workflow_identity_resolver=resolver)
    with pytest.raises(VisualQALineageError) as error:
        selector.select(fixture.project_id, fixture.shot_ids[0], VisualQATargetType.KEYFRAME)
    assert error.value.code is VisualQAFailureCode.ASSET_HASH_MISMATCH


def test_an_unapproved_identity_version_is_rejected(
    graph: tuple[Session, FilesystemBlobStore, VisualQAFixture],
) -> None:
    session, _, fixture = graph
    session.execute(
        update(character_identity_versions)
        .where(character_identity_versions.c.id == fixture.character_identity_version_id)
        .values(status="stale", approved_at=None)
    )
    session.flush()
    selector = AuthoritativeInputSelector(session, shot_workflow_identity_resolver=resolver)
    with pytest.raises(VisualQALineageError) as error:
        selector.select(fixture.project_id, fixture.shot_ids[0], VisualQATargetType.VIDEO)
    assert error.value.code is VisualQAFailureCode.INCOMPATIBLE_REFERENCE_VERSION


def test_a_missing_canonical_video_is_a_structured_failure(
    graph: tuple[Session, FilesystemBlobStore, VisualQAFixture],
) -> None:
    session, _, fixture = graph
    from vidgen.db.animation_models import AnimationGeneratedVideo

    session.execute(
        update(AnimationGeneratedVideo)
        .where(AnimationGeneratedVideo.shot_id == fixture.shot_ids[0])
        .values(selected=False)
    )
    session.flush()
    selector = AuthoritativeInputSelector(session, shot_workflow_identity_resolver=resolver)
    with pytest.raises(VisualQALineageError) as error:
        selector.select(fixture.project_id, fixture.shot_ids[0], VisualQATargetType.VIDEO)
    assert error.value.code is VisualQAFailureCode.MISSING_CANONICAL_VIDEO


# --- identity, reuse and restart ---------------------------------------------
def test_changing_a_material_input_produces_a_new_qa_identity(
    graph: tuple[Session, FilesystemBlobStore, VisualQAFixture],
) -> None:
    session, store, fixture = graph
    first = run_one(session, store, fixture)
    # A different first-pass model is a material input, so the identity changes.
    pipeline = VisualQAPipeline(
        session,
        store,
        FakeVisualAgent(model="fake-visual-qa/2"),
        shot_workflow_identity_resolver=resolver,
    )
    second = asyncio.run(
        pipeline.evaluate_shot(
            project_id=fixture.project_id,
            shot_id=fixture.shot_ids[0],
            target_type=VisualQATargetType.VIDEO,
            idempotency_key="visual-qa-alternate-model",
        )
    )
    assert first.qa_identity != second.qa_identity


def test_reusing_an_idempotency_key_for_different_inputs_is_rejected(
    graph: tuple[Session, FilesystemBlobStore, VisualQAFixture],
) -> None:
    session, store, fixture = graph
    run_one(session, store, fixture, index=0, key="shared-key")
    outcome = asyncio.run(
        run_visual_qa(
            session,
            store,
            project_id=fixture.project_id,
            options=VisualQACommandOptions(
                provider="fake",
                shot_id=fixture.shot_ids[1],
                targets=(VisualQATargetType.VIDEO,),
                idempotency_key="shared-key",
            ),
            identity_resolver=resolver,
        )
    )
    assert outcome.results == ()
    assert [code for _, _, code in outcome.failures] == [VisualQAFailureCode.IDENTITY_CONFLICT]


def test_a_completed_run_is_reused_without_a_second_provider_call(
    graph: tuple[Session, FilesystemBlobStore, VisualQAFixture],
) -> None:
    session, store, fixture = graph
    agent = FakeVisualAgent()
    pipeline = VisualQAPipeline(session, store, agent, shot_workflow_identity_resolver=resolver)
    first = asyncio.run(
        pipeline.evaluate_shot(
            project_id=fixture.project_id,
            shot_id=fixture.shot_ids[0],
            target_type=VisualQATargetType.VIDEO,
            idempotency_key="reuse",
        )
    )
    assert len(agent.calls) == 1
    second = asyncio.run(
        pipeline.evaluate_shot(
            project_id=fixture.project_id,
            shot_id=fixture.shot_ids[0],
            target_type=VisualQATargetType.VIDEO,
            idempotency_key="reuse",
        )
    )
    assert len(agent.calls) == 1, "a completed run never makes a second paid call"
    assert second.qa_run_id == first.qa_run_id
    assert second.score.total == first.score.total
    assert second.cost_microusd == first.cost_microusd


def test_an_interrupted_run_resumes_its_samples_and_attempt(
    graph: tuple[Session, FilesystemBlobStore, VisualQAFixture],
) -> None:
    session, store, fixture = graph
    run_one(session, store, fixture, key="interrupted")
    repository = VisualQARepository(session)
    run = repository.runs_for_shot(fixture.project_id, fixture.shot_ids[0])[0]
    samples = [item.id for item in repository.samples(run.id)]
    attempts = [item.id for item in repository.attempts(run.id)]
    # Simulate a worker that died after the provider call but before completion.
    run.status = "visual_qa_evaluating"
    run.selected_result_id = None
    session.commit()

    agent = FakeVisualAgent()
    pipeline = VisualQAPipeline(session, store, agent, shot_workflow_identity_resolver=resolver)
    asyncio.run(
        pipeline.evaluate_shot(
            project_id=fixture.project_id,
            shot_id=fixture.shot_ids[0],
            target_type=VisualQATargetType.VIDEO,
            idempotency_key="interrupted",
        )
    )
    assert agent.calls == [], "the persisted provider result is replayed, not repurchased"
    resumed = repository.runs_for_shot(fixture.project_id, fixture.shot_ids[0])[0]
    assert [item.id for item in repository.samples(resumed.id)] == samples
    assert [item.id for item in repository.attempts(resumed.id)] == attempts
    assert resumed.status == "visual_qa_complete"


# --- T23 cost integration ----------------------------------------------------
def test_a_provider_attempt_reservation_and_ledger_entry_are_created_once(
    graph: tuple[Session, FilesystemBlobStore, VisualQAFixture],
) -> None:
    session, store, fixture = graph
    before = session.scalar(select(func.count()).select_from(ProviderAttempt)) or 0
    run_one(session, store, fixture, key="cost")
    attempts = list(
        session.scalars(select(ProviderAttempt).where(ProviderAttempt.operation == "visual_qa"))
    )
    assert len(attempts) == 1
    assert attempts[0].status == "SUCCEEDED"
    assert attempts[0].estimated_cost > 0
    ledger = list(
        session.scalars(select(CostLedgerEntry).where(CostLedgerEntry.operation == "visual_qa"))
    )
    assert len(ledger) == 1
    assert ledger[0].actual_amount > 0
    run_one(session, store, fixture, key="cost")
    assert session.scalar(select(func.count()).select_from(ProviderAttempt)) == before + 1, (
        "a reused run never buys a second attempt"
    )


def test_a_budget_denial_stops_the_run_before_the_provider_call(
    graph: tuple[Session, FilesystemBlobStore, VisualQAFixture],
) -> None:
    session, store, fixture = graph
    budget = session.query(ProjectBudget).filter_by(project_id=fixture.project_id).one()
    budget.warning_cap = Decimal("0.000001")
    budget.hard_cap = Decimal("0.000002")
    budget.reserved_amount = Decimal("0")
    budget.committed_amount = Decimal("0")
    session.commit()
    agent = FakeVisualAgent()
    pipeline = VisualQAPipeline(session, store, agent, shot_workflow_identity_resolver=resolver)
    with pytest.raises(BudgetExceededError, match="visual QA denied"):
        asyncio.run(
            pipeline.evaluate_shot(
                project_id=fixture.project_id,
                shot_id=fixture.shot_ids[0],
                target_type=VisualQATargetType.VIDEO,
                idempotency_key="denied",
            )
        )
    assert agent.calls == [], "a denied budget never reaches the provider"


# --- gating ------------------------------------------------------------------
def _hard_failure(shot_id: UUID) -> dict[UUID, FakeDefect]:
    return {
        shot_id: FakeDefect(
            findings=(
                FakeFinding(
                    dimension=VisualQADimension.CHARACTER_IDENTITY,
                    severity="hard_failure",
                    code="wrong_primary_character",
                    summary="wrong character",
                    repair_codes=(VisualQARepairCode.WRONG_CHARACTER_IDENTITY,),
                ),
            ),
            proposed_hard_failure_codes=("WRONG_CHARACTER_IDENTITY",),
        )
    }


def test_keyframe_qa_gates_animation(
    graph: tuple[Session, FilesystemBlobStore, VisualQAFixture],
) -> None:
    session, store, fixture = graph
    defects = _hard_failure(fixture.shot_ids[0])
    with pytest.raises(VisualQABlocked) as blocked:
        asyncio.run(
            evaluate_shot_stage(
                session,
                store,
                project_id=fixture.project_id,
                shot_id=fixture.shot_ids[0],
                target_type=VisualQATargetType.KEYFRAME,
                options=VisualQACommandOptions(provider="fake", fake_defects=defects),
                identity_resolver=resolver,
            )
        )
    assert blocked.value.target is VisualQATargetType.KEYFRAME
    assert "WRONG_CHARACTER_IDENTITY" in blocked.value.repair_codes


def test_video_qa_gates_locking_and_leaves_the_sibling_untouched(
    graph: tuple[Session, FilesystemBlobStore, VisualQAFixture],
) -> None:
    session, store, fixture = graph
    passing = run_one(session, store, fixture, index=1)
    assert passing.outcome is VisualQAOutcome.PASS
    sibling_rows = _rows(session, fixture.shot_ids[1])

    with pytest.raises(VisualQABlocked):
        asyncio.run(
            evaluate_shot_stage(
                session,
                store,
                project_id=fixture.project_id,
                shot_id=fixture.shot_ids[0],
                target_type=VisualQATargetType.VIDEO,
                options=VisualQACommandOptions(
                    provider="fake", fake_defects=_hard_failure(fixture.shot_ids[0])
                ),
                identity_resolver=resolver,
            )
        )
    assert _rows(session, fixture.shot_ids[1]) == sibling_rows
    repository = VisualQARepository(session)
    assert repository.gate(fixture.shot_ids[1], VisualQATargetType.VIDEO) == (
        True,
        "visual_qa_pass",
    )


def _rows(session: Session, shot_id: UUID) -> tuple[object, ...]:
    repository = VisualQARepository(session)
    return tuple(
        (run.id, run.qa_identity, run.final_outcome, run.final_score, run.completed_at)
        for run in repository.runs_for_shot_any_project(shot_id)
    )


def test_a_review_outcome_waits_for_a_human_and_can_be_approved(
    graph: tuple[Session, FilesystemBlobStore, VisualQAFixture],
) -> None:
    session, store, fixture = graph
    # A low identity confidence with a low-confidence adjudicator produces REVIEW.
    defect = FakeDefect(
        dimension_confidence={VisualQADimension.CHARACTER_IDENTITY: 0.4},
        overall_confidence=0.4,
    )
    with pytest.raises(VisualQAReviewRequired) as review:
        asyncio.run(
            evaluate_shot_stage(
                session,
                store,
                project_id=fixture.project_id,
                shot_id=fixture.shot_ids[0],
                target_type=VisualQATargetType.VIDEO,
                options=VisualQACommandOptions(
                    provider="fake", fake_defects={fixture.shot_ids[0]: defect}
                ),
                identity_resolver=resolver,
            )
        )
    repository = VisualQARepository(session)
    run = repository.run(fixture.project_id, review.value.qa_run_id)
    assert run is not None and run.final_outcome == VisualQAOutcome.REVIEW.value
    assert run.hard_failure is False

    service = VisualQAHumanReviewService(session, "owner-a")
    outcome = service.decide(
        run,
        decision="approved",
        reason="identity confirmed by hand",
        row_version=1,
        idempotency_key="review-1",
    )
    session.commit()
    assert outcome.resulting_gate == "visual_qa_human_approved"
    assert repository.gate(run.shot_id, VisualQATargetType.VIDEO)[0] is True
    # The automated result is preserved, not rewritten.
    assert run.final_outcome == VisualQAOutcome.REVIEW.value
    result = repository.canonical_result(run.id)
    assert result is not None and result.outcome == VisualQAOutcome.REVIEW.value


def test_human_review_can_never_clear_a_hard_failure(
    graph: tuple[Session, FilesystemBlobStore, VisualQAFixture],
) -> None:
    session, store, fixture = graph
    with pytest.raises(VisualQABlocked) as blocked:
        asyncio.run(
            evaluate_shot_stage(
                session,
                store,
                project_id=fixture.project_id,
                shot_id=fixture.shot_ids[0],
                target_type=VisualQATargetType.VIDEO,
                options=VisualQACommandOptions(
                    provider="fake", fake_defects=_hard_failure(fixture.shot_ids[0])
                ),
                identity_resolver=resolver,
            )
        )
    repository = VisualQARepository(session)
    run = repository.run(fixture.project_id, blocked.value.qa_run_id)
    assert run is not None and run.hard_failure is True
    with pytest.raises(ReviewError, match="hard failure"):
        VisualQAHumanReviewService(session, "owner-a").decide(
            run,
            decision="approved",
            reason="looks fine to me",
            row_version=1,
            idempotency_key="review-2",
        )


def test_a_deterministic_corruption_never_reaches_the_provider(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'corrupt.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    blob_root = tmp_path / "blobs"
    store = FilesystemBlobStore(blob_root, b"test-secret")
    with factory() as session:
        fixture = build_visual_qa_project(
            session, blob_root, tmp_path, shot_count=1, defects={0: "corrupt"}
        )
        agent = FakeVisualAgent()
        pipeline = VisualQAPipeline(session, store, agent, shot_workflow_identity_resolver=resolver)
        result = asyncio.run(
            pipeline.evaluate_shot(
                project_id=fixture.project_id,
                shot_id=fixture.shot_ids[0],
                target_type=VisualQATargetType.VIDEO,
                idempotency_key="corrupt",
            )
        )
        assert agent.calls == [], "a corrupt asset is never sent to a paid provider"
        assert result.outcome is VisualQAOutcome.FAIL
        assert VisualQARepairCode.DECODE_FAILURE in result.repair_codes
        assert result.sampling_manifest.samples == []
        assert (
            session.scalar(
                select(func.count())
                .select_from(ProviderAttempt)
                .where(ProviderAttempt.operation == "visual_qa")
            )
            == 0
        )


def test_qa_assets_record_full_provenance_and_are_never_overwritten(
    graph: tuple[Session, FilesystemBlobStore, VisualQAFixture],
) -> None:
    session, store, fixture = graph
    result = run_one(session, store, fixture, key="provenance")
    frame_asset_id = result.sampling_manifest.samples[0].frame_asset_id
    assert frame_asset_id is not None
    asset = session.get(Asset, frame_asset_id)
    assert asset is not None
    provenance = asset.generation_parameters
    assert provenance["qa_identity"] == result.qa_identity
    assert provenance["rubric_version"] == result.score.rubric_version
    assert provenance["threshold_version"] == result.score.threshold_version
    assert provenance["target_asset_sha256"] == result.target.target_asset_sha256
    assert provenance["reference_parent_asset_ids"]
    assert provenance["provenance"] == "t20-visual-qa"

    # A repeated run reuses the identical asset rather than writing a new one.
    reused = run_one(session, store, fixture, key="provenance")
    assert reused.sampling_manifest.samples[0].frame_asset_id == frame_asset_id


def test_sample_rows_are_unique_per_run_and_timestamp(
    graph: tuple[Session, FilesystemBlobStore, VisualQAFixture],
) -> None:
    session, store, fixture = graph
    run_one(session, store, fixture, key="unique")
    repository = VisualQARepository(session)
    run = repository.runs_for_shot(fixture.project_id, fixture.shot_ids[0])[0]
    rows = repository.samples(run.id)
    assert [row.sequence for row in rows] == list(range(len(rows)))
    assert len({row.actual_timestamp_us for row in rows}) == len(rows)
    duplicate = VisualQASampleRecord(
        id=uuid4(),
        qa_run_id=run.id,
        sequence=0,
        sample_type="coverage",
        requested_timestamp_us=rows[0].requested_timestamp_us,
        actual_timestamp_us=rows[0].actual_timestamp_us,
        shot_relative_timestamp_us=0,
        frame_sha256="a" * 64,
        source_asset_id=rows[0].source_asset_id,
        selection_reason="duplicate",
        measurements={},
        created_at=rows[0].created_at,
    )
    session.add(duplicate)
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_exactly_one_canonical_result_per_run(
    graph: tuple[Session, FilesystemBlobStore, VisualQAFixture],
) -> None:
    session, store, fixture = graph
    run_one(session, store, fixture, key="canonical")
    repository = VisualQARepository(session)
    run = repository.runs_for_shot(fixture.project_id, fixture.shot_ids[0])[0]
    canonical = [item for item in repository.results(run.id) if item.canonical]
    assert len(canonical) == 1
    assert run.selected_result_id == canonical[0].id


def test_attempt_numbers_are_unique_per_type(
    graph: tuple[Session, FilesystemBlobStore, VisualQAFixture],
) -> None:
    session, store, fixture = graph
    run_one(session, store, fixture, key="attempts")
    repository = VisualQARepository(session)
    run = repository.runs_for_shot(fixture.project_id, fixture.shot_ids[0])[0]
    attempt = repository.attempts(run.id)[0]
    session.add(
        VisualQAAttempt(
            qa_run_id=run.id,
            attempt_number=attempt.attempt_number,
            attempt_type=attempt.attempt_type,
            attempt_identity="c" * 64,
            provider="fake",
            model="fake",
            status="succeeded",
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_a_run_row_cannot_record_a_hard_failure_without_failing(
    graph: tuple[Session, FilesystemBlobStore, VisualQAFixture],
) -> None:
    session, store, fixture = graph
    run_one(session, store, fixture, key="constraint")
    repository = VisualQARepository(session)
    run = repository.runs_for_shot(fixture.project_id, fixture.shot_ids[0])[0]
    with pytest.raises(IntegrityError):
        session.execute(
            update(VisualQARun)
            .where(VisualQARun.id == run.id)
            .values(hard_failure=True, final_outcome="PASS")
        )
    session.rollback()
