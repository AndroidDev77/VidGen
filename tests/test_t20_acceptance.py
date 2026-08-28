"""The mandatory T20 acceptance test.

One project, approved T19 references, a canonical T13 storyboard with a normal
and a hero shot, deterministic T14/T15 fixtures, and the whole T20 pipeline run
end to end against the deterministic fake visual agent.

Nothing here makes a paid provider call.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

import vidgen.db  # noqa: F401  - registers every table on Base.metadata
from services.qa.commands import (
    VisualQABlocked,
    VisualQACommandOptions,
    evaluate_shot_stage,
    run_visual_qa,
)
from services.qa.fake_visual_agent import FakeDefect, FakeFinding
from services.renderer.selection import RenderLineageError, select_authoritative_inputs
from tests.visual_qa_fixtures import VisualQAFixture, build_visual_qa_project
from vidgen.contracts.visual_qa import (
    VisualQADimension,
    VisualQAOutcome,
    VisualQARepairCode,
    VisualQATargetType,
)
from vidgen.db.base import Base
from vidgen.db.cost_models import CostLedgerEntry, CostReservation, ProviderAttempt
from vidgen.db.models import Asset
from vidgen.db.visual_qa_models import (
    VisualQAAttempt,
    VisualQAEvidenceRecord,
    VisualQAResultRecord,
    VisualQARun,
    VisualQASampleRecord,
)
from vidgen.db.visual_qa_repository import VisualQARepository
from vidgen.storage.blob import FilesystemBlobStore

HERO_INDEX = 1
NORMAL_INDEX = 0
FAILING_INDEX = 2


def identity_resolver(_session: Session, _storyboard: object, _shot: object) -> str:
    """A stable T16 identity for the fixture graph."""
    return "a" * 64


@pytest.fixture
def acceptance(tmp_path: Path) -> Iterator[tuple[Session, FilesystemBlobStore, VisualQAFixture]]:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'acceptance.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    blob_root = tmp_path / "blobs"
    store = FilesystemBlobStore(blob_root, b"test-secret")
    with factory() as session:
        fixture = build_visual_qa_project(session, blob_root, tmp_path, shot_count=3)
        yield session, store, fixture


def _defects(fixture: VisualQAFixture) -> dict[UUID, FakeDefect]:
    """A passing normal shot, a hero shot below 90, and one hard identity failure."""
    return {
        # The hero shot scores 87: above the normal threshold, below the hero one.
        fixture.shot_ids[HERO_INDEX]: FakeDefect(
            dimension_scores=dict.fromkeys(VisualQADimension, 87.0)
        ),
        # A high-scoring shot with one hard identity failure. The numeric score
        # must not be able to override it.
        fixture.shot_ids[FAILING_INDEX]: FakeDefect(
            dimension_scores=dict.fromkeys(VisualQADimension, 99.0),
            findings=(
                FakeFinding(
                    dimension=VisualQADimension.CHARACTER_IDENTITY,
                    severity="hard_failure",
                    code="wrong_primary_character",
                    summary="The subject is not the approved identity version.",
                    repair_codes=(VisualQARepairCode.WRONG_CHARACTER_IDENTITY,),
                    confidence=0.96,
                    proposed_correction="Rebind the approved T19 identity reference.",
                ),
            ),
            proposed_hard_failure_codes=("WRONG_CHARACTER_IDENTITY",),
        ),
    }


async def _run(
    session: Session,
    store: FilesystemBlobStore,
    fixture: VisualQAFixture,
    shot_index: int,
    defects: dict[UUID, FakeDefect],
) -> object:
    return await run_visual_qa(
        session,
        store,
        project_id=fixture.project_id,
        options=VisualQACommandOptions(
            provider="fake", fake_defects=defects, shot_id=fixture.shot_ids[shot_index]
        ),
        identity_resolver=identity_resolver,
    )


def test_t20_acceptance(
    acceptance: tuple[Session, FilesystemBlobStore, VisualQAFixture],
) -> None:
    session, store, fixture = acceptance
    defects = _defects(fixture)
    counts = lambda model: session.scalar(select(func.count()).select_from(model))  # noqa: E731

    # 4-8. Start keyframe and video QA for every prepared shot: sample exact
    # deterministic timestamps, run the deterministic checks, run the fake
    # visual agent, and recompute the weighted score in application code.
    outcomes = {
        index: asyncio.run(_run(session, store, fixture, index, defects)) for index in range(3)
    }
    for index, outcome in outcomes.items():
        assert outcome.failures == (), f"shot {index} failed selection: {outcome.failures}"
        assert len(outcome.results) == 2

    by_target = {
        (result.target.shot_sequence, result.target.target_type): result
        for outcome in outcomes.values()
        for result in outcome.results
    }

    # 5. Sampling is deterministic and exact: requested and actual timestamps are
    # both recorded, unique, and in canonical order.
    video = by_target[(NORMAL_INDEX, VisualQATargetType.VIDEO)]
    timestamps = [item.actual_timestamp_us for item in video.sampling_manifest.samples]
    assert timestamps == sorted(timestamps)
    assert len(set(timestamps)) == len(timestamps)
    assert all(item.selection_reason for item in video.sampling_manifest.samples), (
        "every sample records why it was selected"
    )

    # 6-8. Deterministic checks ran, and the score is the sum of the recomputed
    # weighted contributions rather than anything the provider supplied.
    assert video.deterministic_report.metrics
    recomputed = sum(
        item.weighted_contribution for item in video.score.dimensions if item.applicable
    )
    assert video.score.total == pytest.approx(recomputed)

    # 9. A normal shot passes at 85.
    normal = by_target[(NORMAL_INDEX, VisualQATargetType.VIDEO)]
    assert normal.score.pass_threshold == 85
    assert normal.outcome is VisualQAOutcome.PASS

    # 10. A hero shot below 90 does not pass.
    hero = by_target[(HERO_INDEX, VisualQATargetType.VIDEO)]
    assert hero.target.importance.value == "hero"
    assert hero.score.pass_threshold == 90
    assert hero.score.total == pytest.approx(87.0)
    assert hero.outcome is VisualQAOutcome.FAIL
    assert VisualQARepairCode.WRONG_CHARACTER_IDENTITY in hero.repair_codes

    # 11-12. One hard identity failure, and its high numeric score cannot
    # override it.
    failed = by_target[(FAILING_INDEX, VisualQATargetType.VIDEO)]
    assert failed.score.total == pytest.approx(99.0)
    assert failed.score.total > failed.score.pass_threshold
    assert failed.hard_failure is True
    assert failed.outcome is VisualQAOutcome.FAIL
    assert "WRONG_CHARACTER_IDENTITY" in failed.hard_failure_codes

    # 13. Exact evidence frames and timestamps persist.
    repository = VisualQARepository(session)
    run = repository.run_by_identity(failed.qa_identity)
    assert run is not None
    result = repository.canonical_result(run.id)
    assert result is not None
    evidence = repository.evidence(result.id)
    assert evidence, "a hard failure persists its evidence"
    assert any(item.source_relative_timestamp_us is not None for item in evidence)
    assert any(item.sample_id is not None for item in evidence)

    # 14. Repair codes persist on the result row.
    assert result.repair_codes
    assert "WRONG_CHARACTER_IDENTITY" in result.hard_failure_codes

    # 15. The failed shot cannot reach LOCKED: its T16 gate raises.
    with pytest.raises(VisualQABlocked) as blocked:
        asyncio.run(
            evaluate_shot_stage(
                session,
                store,
                project_id=fixture.project_id,
                shot_id=fixture.shot_ids[FAILING_INDEX],
                target_type=VisualQATargetType.VIDEO,
                options=VisualQACommandOptions(provider="fake", fake_defects=defects),
                identity_resolver=identity_resolver,
            )
        )
    assert "WRONG_CHARACTER_IDENTITY" in blocked.value.repair_codes
    assert blocked.value.retryable is False

    # 16. The passing sibling stays passing and is not rerun: its gate opens and
    # its persisted rows are untouched.
    sibling_before = _shot_snapshot(session, fixture.shot_ids[NORMAL_INDEX])
    passed, reason = repository.gate(fixture.shot_ids[NORMAL_INDEX], VisualQATargetType.VIDEO)
    assert (passed, reason) == (True, "visual_qa_pass")
    assert _shot_snapshot(session, fixture.shot_ids[NORMAL_INDEX]) == sibling_before

    # 17. T17 render eligibility is blocked while a shot is failing.
    with pytest.raises(RenderLineageError) as render_error:
        select_authoritative_inputs(session, fixture.project_id)
    assert render_error.value.code == "visual_qa_failed"
    assert render_error.value.retryable is False

    # 18-19. Retrying the identical QA request reuses everything: no new samples,
    # results, provider attempts, reservations, assets, or ledger charges.
    before = (
        counts(VisualQARun),
        counts(VisualQASampleRecord),
        counts(VisualQAAttempt),
        counts(VisualQAResultRecord),
        counts(VisualQAEvidenceRecord),
        counts(ProviderAttempt),
        counts(CostReservation),
        counts(CostLedgerEntry),
        counts(Asset),
    )
    repeated = {
        index: asyncio.run(_run(session, store, fixture, index, defects)) for index in range(3)
    }
    after = (
        counts(VisualQARun),
        counts(VisualQASampleRecord),
        counts(VisualQAAttempt),
        counts(VisualQAResultRecord),
        counts(VisualQAEvidenceRecord),
        counts(ProviderAttempt),
        counts(CostReservation),
        counts(CostLedgerEntry),
        counts(Asset),
    )
    assert before == after, "an identical QA request must reuse every persisted row and asset"
    for index, outcome in repeated.items():
        original = outcomes[index]
        assert [item.outcome for item in outcome.results] == [
            item.outcome for item in original.results
        ]
        assert [item.score.total for item in outcome.results] == [
            item.score.total for item in original.results
        ]
        assert [item.qa_run_id for item in outcome.results] == [
            item.qa_run_id for item in original.results
        ]

    # 20. No T21 repair action was executed: every result only recommends one.
    for outcome in outcomes.values():
        for result in outcome.results:
            assert result.recommendation.executed is False


def _shot_snapshot(session: Session, shot_id: UUID) -> tuple[object, ...]:
    """The persisted QA state of one shot, used to prove a sibling was not rerun."""
    repository = VisualQARepository(session)
    runs = sorted(
        (run for run in repository.runs_for_shot_any_project(shot_id)),
        key=lambda run: (run.target_type, str(run.id)),
    )
    return tuple(
        (
            run.id,
            run.qa_identity,
            run.final_outcome,
            run.final_score,
            run.completed_at,
            tuple(sample.id for sample in repository.samples(run.id)),
            tuple(attempt.id for attempt in repository.attempts(run.id)),
        )
        for run in runs
    )
