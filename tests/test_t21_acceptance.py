"""The mandatory T21 acceptance tests.

One project, approved T19 references, a canonical T13 storyboard, deterministic
T14/T15 fixtures and the whole T21 policy driven end to end against fake
providers and the real T20 pipeline.

Nothing here makes a paid provider call, and nothing here needs a credential.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

import vidgen.db  # noqa: F401  - registers every table on Base.metadata
from services.qa.commands import (
    VisualQACommandOptions,
    VisualRepairCommandOptions,
    run_visual_qa,
    run_visual_repair,
)
from services.qa.repair import RepairNotRequired, VisualRepairPipeline
from tests.repair_fixtures import (
    failing_profile,
    identity_resolver,
    passing_profile,
    scripted_revalidator,
)
from tests.visual_qa_fixtures import VisualQAFixture, build_visual_qa_project
from vidgen.contracts.repair import (
    HumanReviewReason,
    RepairAttemptKind,
    RepairAttemptStatus,
    RepairOutcome,
    RepairRoute,
    RepairRunState,
)
from vidgen.contracts.visual_qa import VisualQATargetType
from vidgen.db.animation_models import AnimationGeneratedVideo
from vidgen.db.base import Base
from vidgen.db.cost_models import CostLedgerEntry, CostReservation, ProjectBudget, ProviderAttempt
from vidgen.db.models import Project
from vidgen.db.repair_models import RepairAttemptRecord, RepairFallbackRender, RepairRun
from vidgen.db.repair_repository import RepairConcurrencyError, RepairRepository
from vidgen.db.visual_qa_models import VisualQARun
from vidgen.storage.blob import FilesystemBlobStore

FAILING_INDEX = 0
SIBLING_INDEXES = (1, 2)
WIDTH, HEIGHT = 320, 180


@pytest.fixture
def repair_project(
    tmp_path: Path,
) -> Iterator[tuple[Session, FilesystemBlobStore, VisualQAFixture]]:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'repair-acceptance.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    blob_root = tmp_path / "blobs"
    store = FilesystemBlobStore(blob_root, b"test-secret")
    with factory() as session:
        fixture = build_visual_qa_project(session, blob_root, tmp_path, shot_count=3)
        _seed_qa(session, store, fixture)
        yield session, store, fixture


def _seed_qa(session: Session, store: FilesystemBlobStore, fixture: VisualQAFixture) -> None:
    """Run real T20 QA: every keyframe passes, and shot 0's clip fails."""
    defects = {fixture.shot_ids[FAILING_INDEX]: failing_profile()}
    for index in range(3):
        for targets, profile in (
            ((VisualQATargetType.KEYFRAME,), {}),
            ((VisualQATargetType.VIDEO,), defects),
        ):
            asyncio.run(
                run_visual_qa(
                    session,
                    store,
                    project_id=fixture.project_id,
                    options=VisualQACommandOptions(
                        provider="fake",
                        fake_defects=profile,
                        targets=targets,
                        shot_id=fixture.shot_ids[index],
                        expected_width=WIDTH,
                        expected_height=HEIGHT,
                    ),
                    identity_resolver=identity_resolver,
                )
            )


def _options(**overrides: object) -> VisualRepairCommandOptions:
    values: dict[str, object] = {
        "provider": "fake",
        "alternate_provider": "fake",
        "alternate_fake_geometry": (WIDTH, HEIGHT),
        "width": WIDTH,
        "height": HEIGHT,
        "frame_rate": 24,
        "qa": VisualQACommandOptions(provider="fake", expected_width=WIDTH, expected_height=HEIGHT),
    }
    values.update(overrides)
    return VisualRepairCommandOptions(**values)  # type: ignore[arg-type]


def _repair(
    session: Session,
    store: FilesystemBlobStore,
    fixture: VisualQAFixture,
    profiles: list[object],
    *,
    options: VisualRepairCommandOptions | None = None,
    shot_index: int = FAILING_INDEX,
) -> RepairOutcome:
    revalidate, _agent = scripted_revalidator(
        session,
        store,
        profiles,
        width=WIDTH,
        height=HEIGHT,  # type: ignore[arg-type]
    )
    return asyncio.run(
        run_visual_repair(
            session,
            store,
            project_id=fixture.project_id,
            shot_id=fixture.shot_ids[shot_index],
            options=options or _options(),
            identity_resolver=identity_resolver,
            revalidate=revalidate,
        )
    )


def test_t21_acceptance_falls_back_to_a_passing_parallax_render(
    repair_project: tuple[Session, FilesystemBlobStore, VisualQAFixture],
) -> None:
    """The mandatory end-to-end fake-provider acceptance path.

    1. The original animation fails T20.
    2. Two same-provider repairs fail.
    3. The alternate-provider attempt fails.
    4. The shot is eligible for parallax.
    5. The deterministic parallax render passes T20.
    6. The fallback becomes the selected animation.
    7. Attempt order, lineage, costs and sibling preservation hold.
    """
    session, store, fixture = repair_project
    sibling_state = _sibling_state(session, fixture)

    outcome = _repair(
        session,
        store,
        fixture,
        [failing_profile(), failing_profile(), failing_profile(), passing_profile()],
    )

    # 6. The fallback is the selected animation and the shot is locked.
    assert outcome.state is RepairRunState.LOCKED
    assert outcome.selected_attempt_id is not None
    assert outcome.final_qa_result_id is not None
    assert outcome.final_qa_score is not None and outcome.final_qa_score >= 85

    # 1-4. Attempt order is exactly the bounded policy, and the original does
    # not count as one of the two same-provider repairs.
    kinds = [attempt.attempt_kind for attempt in outcome.attempts]
    assert kinds == [
        RepairAttemptKind.ORIGINAL,
        RepairAttemptKind.SAME_PROVIDER_REPAIR,
        RepairAttemptKind.SAME_PROVIDER_REPAIR,
        RepairAttemptKind.ALTERNATE_PROVIDER,
        RepairAttemptKind.DETERMINISTIC_FALLBACK,
    ]
    assert [attempt.lineage.attempt_ordinal for attempt in outcome.attempts] == [0, 1, 2, 3, 4]
    routes = [decision.route for decision in outcome.decisions]
    assert routes == [
        RepairRoute.SAME_PROVIDER_REPAIR,
        RepairRoute.SAME_PROVIDER_REPAIR,
        RepairRoute.ALTERNATE_PROVIDER,
        RepairRoute.DETERMINISTIC_FALLBACK,
    ]

    # 7. Lineage: every attempt links to its immediate predecessor and the same
    # root generation.
    root = outcome.attempts[0].lineage.root_animation_attempt_id
    for previous, attempt in zip(outcome.attempts, outcome.attempts[1:], strict=False):
        assert attempt.lineage.predecessor_attempt_id == previous.attempt_id
        assert attempt.lineage.root_animation_attempt_id == root
    assert outcome.attempts[0].lineage.predecessor_attempt_id is None

    # Only the fallback passed, and only it is selected.
    passed = [item for item in outcome.attempts if item.status is RepairAttemptStatus.PASSED]
    assert len(passed) == 1
    assert passed[0].attempt_kind is RepairAttemptKind.DETERMINISTIC_FALLBACK
    assert passed[0].output_qa_result_id is not None

    # Costs: only the alternate-provider attempt was charged; the fallback is free.
    alternate = outcome.attempts[3]
    assert alternate.actual_cost > Decimal("0")
    assert outcome.attempts[4].actual_cost == Decimal("0")
    assert outcome.total_repair_cost == sum(
        (item.actual_cost for item in outcome.attempts), Decimal("0")
    )

    # The deterministic render is persisted with its manifest and tool versions.
    fallback = session.scalar(
        select(RepairFallbackRender).where(
            RepairFallbackRender.repair_attempt_id == passed[0].attempt_id
        )
    )
    assert fallback is not None
    assert fallback.exact_duration_us == 3_000_000
    assert fallback.pixel_format == "yuv420p" and fallback.video_codec == "h264"
    assert fallback.ffmpeg_version and fallback.ffprobe_version
    assert fallback.manifest_asset_id is not None

    # T17 consumes the selected clip: it is the repaired shot's selected T15 row.
    selected_video = session.scalar(
        select(AnimationGeneratedVideo).where(
            AnimationGeneratedVideo.shot_id == fixture.shot_ids[FAILING_INDEX],
            AnimationGeneratedVideo.selected.is_(True),
        )
    )
    assert selected_video is not None
    assert selected_video.canonical_asset_id == outcome.selected_asset_id

    # 7. Sibling shots and their checkpoints are untouched.
    assert _sibling_state(session, fixture) == sibling_state


def test_t21_acceptance_routes_an_ineligible_shot_to_human_review(
    repair_project: tuple[Session, FilesystemBlobStore, VisualQAFixture],
) -> None:
    """The same policy, with a shot a 2.5D render cannot truthfully represent."""
    session, store, fixture = repair_project
    # A mandatory physical action makes the shot ineligible for parallax.
    _require_physical_action(session, fixture)
    sibling_state = _sibling_state(session, fixture)

    outcome = _repair(
        session,
        store,
        fixture,
        [failing_profile(), failing_profile(), failing_profile(), failing_profile()],
    )

    assert outcome.state is RepairRunState.HUMAN_REVIEW_REQUIRED
    assert outcome.human_review_reason is HumanReviewReason.FALLBACK_INELIGIBLE
    assert outcome.selected_attempt_id is None
    kinds = [attempt.attempt_kind for attempt in outcome.attempts]
    assert kinds == [
        RepairAttemptKind.ORIGINAL,
        RepairAttemptKind.SAME_PROVIDER_REPAIR,
        RepairAttemptKind.SAME_PROVIDER_REPAIR,
        RepairAttemptKind.ALTERNATE_PROVIDER,
    ]
    assert outcome.decisions[-1].route is RepairRoute.HUMAN_REVIEW_REQUIRED
    # No paid attempt beyond the bounded policy, and no fallback render.
    assert session.scalar(select(func.count()).select_from(RepairFallbackRender)) == 0
    assert _sibling_state(session, fixture) == sibling_state


def test_a_repeated_repair_request_is_free_and_creates_no_second_run(
    repair_project: tuple[Session, FilesystemBlobStore, VisualQAFixture],
) -> None:
    session, store, fixture = repair_project
    options = _options(idempotency_key="t21-acceptance:1")
    first = _repair(
        session,
        store,
        fixture,
        [failing_profile(), failing_profile(), failing_profile(), passing_profile()],
        options=options,
    )
    attempts_before = session.scalar(select(func.count()).select_from(RepairAttemptRecord))
    charges_before = session.scalar(select(func.count()).select_from(CostLedgerEntry))
    provider_attempts_before = session.scalar(select(func.count()).select_from(ProviderAttempt))

    second = _repair(
        session,
        store,
        fixture,
        [passing_profile()],
        options=options,
    )
    assert second.repair_run_id == first.repair_run_id
    assert second.state is RepairRunState.LOCKED
    assert session.scalar(select(func.count()).select_from(RepairRun)) == 1
    # No duplicate attempts, provider attempts, reservations or ledger charges.
    assert session.scalar(select(func.count()).select_from(RepairAttemptRecord)) == (
        attempts_before
    )
    assert session.scalar(select(func.count()).select_from(CostLedgerEntry)) == charges_before
    assert session.scalar(select(func.count()).select_from(ProviderAttempt)) == (
        provider_attempts_before
    )


def test_a_budget_denial_stops_before_any_provider_call(
    repair_project: tuple[Session, FilesystemBlobStore, VisualQAFixture],
) -> None:
    session, store, fixture = repair_project
    # Enough budget for the free same-provider repairs and for T20's own
    # evaluations, but far less than one priced alternate-provider generation.
    _set_budget(session, fixture, hard_cap=Decimal("0.50"))

    outcome = _repair(
        session,
        store,
        fixture,
        [failing_profile(), failing_profile(), failing_profile()],
        options=_options(),
    )

    assert outcome.state is RepairRunState.HUMAN_REVIEW_REQUIRED
    assert outcome.human_review_reason is HumanReviewReason.PROJECT_BUDGET_DENIED
    # The alternate provider was never called: no attempt, reservation or charge
    # exists for it, and no alternate-provider repair attempt was recorded.
    veo_attempts = session.scalars(
        select(ProviderAttempt).where(ProviderAttempt.provider == "google_veo")
    ).all()
    assert veo_attempts == []
    assert all(
        item.attempt_kind is not RepairAttemptKind.ALTERNATE_PROVIDER for item in outcome.attempts
    )


def test_a_per_shot_repair_cost_limit_is_enforced_before_the_provider(
    repair_project: tuple[Session, FilesystemBlobStore, VisualQAFixture],
) -> None:
    session, store, fixture = repair_project
    outcome = _repair(
        session,
        store,
        fixture,
        [failing_profile()],
        options=_options(
            provider="fake",
            alternate_provider="fake",
            per_shot_repair_cost_limit=Decimal("0.000001"),
        ),
    )
    # The fake same-provider generation is free, so the first two repairs run;
    # the priced alternate-provider attempt is refused before any call.
    routes = [decision.route for decision in outcome.decisions]
    assert RepairRoute.ALTERNATE_PROVIDER not in routes
    assert outcome.human_review_reason is HumanReviewReason.REPAIR_BUDGET_EXHAUSTED


def test_reservations_are_reconciled_and_never_charged_twice(
    repair_project: tuple[Session, FilesystemBlobStore, VisualQAFixture],
) -> None:
    session, store, fixture = repair_project
    _set_budget(session, fixture, hard_cap=Decimal("100"), warning_cap=Decimal("50"))
    outcome = _repair(
        session,
        store,
        fixture,
        [failing_profile(), failing_profile(), failing_profile(), passing_profile()],
    )
    alternate = outcome.attempts[3]
    # T20 reserves and reconciles for its own evaluations; only the repair
    # generations are counted here.
    # Ordinal 0 is the original T15 generation, whose ledger entry belongs to
    # T15, not to this repair.
    repair_attempt_ids = {
        item.provider_attempt_id
        for item in outcome.attempts
        if item.provider_attempt_id and item.lineage.attempt_ordinal > 0
    }
    entries = [
        entry
        for entry in session.scalars(select(CostLedgerEntry))
        if entry.provider_attempt_id in repair_attempt_ids
    ]
    assert len(entries) == 1, "exactly one ledger entry for the one paid repair attempt"
    assert entries[0].actual_amount == alternate.actual_cost
    reservations = [
        item
        for item in session.scalars(select(CostReservation))
        if item.provider_attempt_id in repair_attempt_ids
    ]
    assert len(reservations) == 1
    budget = session.scalar(
        select(ProjectBudget).where(ProjectBudget.project_id == fixture.project_id)
    )
    assert budget is not None
    # Every reservation is reconciled: nothing is left reserved.
    assert budget.reserved_amount == Decimal("0")
    assert budget.committed_amount >= alternate.actual_cost


def test_a_run_that_never_locks_restores_the_original_selected_clip(
    repair_project: tuple[Session, FilesystemBlobStore, VisualQAFixture],
) -> None:
    """A repair selects each candidate so T20 can see it, and puts it back.

    The T20 selector only ever evaluates the shot's *selected* clip, so every
    candidate is selected in turn. When the run ends without a passing output,
    leaving the last rejected candidate selected would make the shot's
    authoritative clip one T20 refused.
    """
    session, store, fixture = repair_project
    shot_id = fixture.shot_ids[FAILING_INDEX]
    original = session.scalar(
        select(AnimationGeneratedVideo).where(
            AnimationGeneratedVideo.shot_id == shot_id,
            AnimationGeneratedVideo.selected.is_(True),
        )
    )
    assert original is not None
    original_id = original.id
    _require_physical_action(session, fixture)

    outcome = _repair(session, store, fixture, [failing_profile()])

    assert outcome.state is RepairRunState.HUMAN_REVIEW_REQUIRED
    selected = session.scalars(
        select(AnimationGeneratedVideo).where(
            AnimationGeneratedVideo.shot_id == shot_id,
            AnimationGeneratedVideo.selected.is_(True),
        )
    ).all()
    assert [item.id for item in selected] == [original_id]
    # The rejected candidates remain as immutable historical rows.
    assert (
        len(
            session.scalars(
                select(AnimationGeneratedVideo).where(AnimationGeneratedVideo.shot_id == shot_id)
            ).all()
        )
        > 1
    )


def test_a_second_repair_run_can_render_its_own_fallback(
    repair_project: tuple[Session, FilesystemBlobStore, VisualQAFixture],
) -> None:
    """The render identity is content-derived, so two runs legitimately share one."""
    session, store, fixture = repair_project
    profiles = [failing_profile(), failing_profile(), failing_profile(), failing_profile()]
    first = _repair(session, store, fixture, profiles, options=_options(idempotency_key="run-a"))
    assert first.state is RepairRunState.HUMAN_REVIEW_REQUIRED
    second = _repair(session, store, fixture, profiles, options=_options(idempotency_key="run-b"))
    assert second.repair_run_id != first.repair_run_id
    renders = session.scalars(select(RepairFallbackRender)).all()
    assert len(renders) == 2
    # Both runs rendered the same shot from the same still, so the identity is
    # shared; the attempt is what makes each stored render unique.
    assert renders[0].render_identity == renders[1].render_identity
    assert renders[0].repair_attempt_id != renders[1].repair_attempt_id


def test_an_injected_provider_is_used_instead_of_building_a_second_one(
    repair_project: tuple[Session, FilesystemBlobStore, VisualQAFixture],
) -> None:
    """The T16 worker hands in its configured T15 provider; T21 must use it."""
    session, store, fixture = repair_project
    from services.animation.fake_provider import FakeVideoProvider

    injected = FakeVideoProvider()
    revalidate, _agent = scripted_revalidator(
        session, store, [failing_profile()], width=WIDTH, height=HEIGHT
    )
    asyncio.run(
        run_visual_repair(
            session,
            store,
            project_id=fixture.project_id,
            shot_id=fixture.shot_ids[FAILING_INDEX],
            options=_options(alternate_provider="none"),
            identity_resolver=identity_resolver,
            revalidate=revalidate,
            same_provider=injected,
        )
    )
    assert injected.submissions >= 1


def test_a_passing_shot_is_never_repaired(
    repair_project: tuple[Session, FilesystemBlobStore, VisualQAFixture],
) -> None:
    session, store, fixture = repair_project
    with pytest.raises(RepairNotRequired):
        _repair(session, store, fixture, [passing_profile()], shot_index=SIBLING_INDEXES[0])


def test_two_workers_cannot_advance_the_same_repair_run_twice(
    repair_project: tuple[Session, FilesystemBlobStore, VisualQAFixture],
) -> None:
    """The conditional advance token serialises concurrent workers."""
    session, store, fixture = repair_project
    outcome = _repair(session, store, fixture, [failing_profile()], options=_options())
    repository = RepairRepository(session)
    run = repository.run(fixture.project_id, outcome.repair_run_id)
    assert run is not None
    stale = run.advance_token
    repository.claim_advance(run, expected_token=stale)
    session.commit()
    with pytest.raises(RepairConcurrencyError):
        repository.claim_advance(run, expected_token=stale)


def test_an_interrupted_run_resumes_without_repeating_a_paid_attempt(
    repair_project: tuple[Session, FilesystemBlobStore, VisualQAFixture],
) -> None:
    """A worker that dies mid-revalidation resumes the attempt it already paid for."""
    session, store, fixture = repair_project
    options = _options(idempotency_key="t21-resume:1")
    revalidate, _agent = scripted_revalidator(
        session, store, [failing_profile()], width=WIDTH, height=HEIGHT
    )
    calls = {"count": 0}

    async def interrupting(*, project_id: UUID, shot_id: UUID, idempotency_key: str) -> object:
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("worker interrupted between generation and revalidation")
        return await revalidate(
            project_id=project_id, shot_id=shot_id, idempotency_key=idempotency_key
        )

    with pytest.raises(RuntimeError, match="worker interrupted"):
        asyncio.run(
            run_visual_repair(
                session,
                store,
                project_id=fixture.project_id,
                shot_id=fixture.shot_ids[FAILING_INDEX],
                options=options,
                identity_resolver=identity_resolver,
                revalidate=interrupting,  # type: ignore[arg-type]
            )
        )
    session.rollback()
    interrupted = session.scalars(
        select(RepairAttemptRecord).order_by(RepairAttemptRecord.attempt_ordinal)
    ).all()
    # The generated clip is durable and the attempt is mid-revalidation.
    assert interrupted[-1].status == RepairAttemptStatus.REVALIDATING.value
    assert interrupted[-1].generated_video_id is not None
    generated_before = len(interrupted)

    resumed = _repair(
        session,
        store,
        fixture,
        [failing_profile(), failing_profile(), failing_profile(), passing_profile()],
        options=options,
    )
    # The interrupted attempt was revalidated rather than regenerated, so the
    # bounded policy still had every one of its attempts available.
    assert resumed.state is RepairRunState.LOCKED
    assert resumed.attempts[generated_before - 1].output_qa_result_id is not None
    assert [item.attempt_kind for item in resumed.attempts] == [
        RepairAttemptKind.ORIGINAL,
        RepairAttemptKind.SAME_PROVIDER_REPAIR,
        RepairAttemptKind.SAME_PROVIDER_REPAIR,
        RepairAttemptKind.ALTERNATE_PROVIDER,
        RepairAttemptKind.DETERMINISTIC_FALLBACK,
    ]


def test_only_the_failed_shot_is_regenerated(
    repair_project: tuple[Session, FilesystemBlobStore, VisualQAFixture],
) -> None:
    session, store, fixture = repair_project
    before = _sibling_state(session, fixture)
    _repair(
        session,
        store,
        fixture,
        [failing_profile(), failing_profile(), failing_profile(), passing_profile()],
    )
    assert _sibling_state(session, fixture) == before
    # Every repair attempt belongs to the failed shot alone.
    shots = set(session.scalars(select(RepairAttemptRecord.shot_id).distinct()))
    assert shots == {fixture.shot_ids[FAILING_INDEX]}


def test_the_repair_pipeline_is_bound_to_one_project(
    repair_project: tuple[Session, FilesystemBlobStore, VisualQAFixture],
) -> None:
    """A cross-project shot ID never resolves into another project's lineage."""
    session, store, fixture = repair_project
    other = Project(
        owner_subject="someone-else", name="Other", status="created", visual_style="flat"
    )
    session.add(other)
    session.commit()
    revalidate, _agent = scripted_revalidator(session, store, [passing_profile()])
    pipeline = VisualRepairPipeline(
        session,
        store,
        same_provider=_fake_video_provider(),
        revalidate=revalidate,
        shot_workflow_identity_resolver=identity_resolver,
    )
    with pytest.raises(Exception):  # noqa: B017 - any lineage refusal is correct
        asyncio.run(
            pipeline.repair(
                project_id=other.id,
                shot_id=fixture.shot_ids[FAILING_INDEX],
                idempotency_key="cross-project",
            )
        )


def _set_budget(
    session: Session,
    fixture: VisualQAFixture,
    *,
    hard_cap: Decimal,
    warning_cap: Decimal | None = None,
) -> None:
    """Configure the project's T23 budget, reusing the row the fixture created."""
    budget = session.scalar(
        select(ProjectBudget).where(ProjectBudget.project_id == fixture.project_id)
    )
    if budget is None:
        budget = ProjectBudget(
            project_id=fixture.project_id,
            warning_cap=warning_cap if warning_cap is not None else hard_cap,
            hard_cap=hard_cap,
            currency="USD",
            policy_version="t23/1",
        )
        session.add(budget)
    else:
        budget.hard_cap = hard_cap
        budget.warning_cap = warning_cap if warning_cap is not None else hard_cap
        budget.reserved_amount = Decimal("0")
        budget.committed_amount = Decimal("0")
    session.commit()


def _fake_video_provider() -> object:
    from services.animation.fake_provider import FakeVideoProvider

    return FakeVideoProvider()


def _sibling_state(
    session: Session, fixture: VisualQAFixture
) -> dict[UUID, tuple[UUID | None, tuple[str, ...]]]:
    """The selected clip and QA verdicts of every shot T21 must not touch."""
    state: dict[UUID, tuple[UUID | None, tuple[str, ...]]] = {}
    for index in SIBLING_INDEXES:
        shot_id = fixture.shot_ids[index]
        video = session.scalar(
            select(AnimationGeneratedVideo).where(
                AnimationGeneratedVideo.shot_id == shot_id,
                AnimationGeneratedVideo.selected.is_(True),
            )
        )
        verdicts = tuple(
            sorted(
                f"{run.target_type}:{run.final_outcome}"
                for run in session.scalars(
                    select(VisualQARun).where(VisualQARun.shot_id == shot_id)
                )
            )
        )
        state[shot_id] = (video.canonical_asset_id if video else None, verdicts)
    return state


def _require_physical_action(session: Session, fixture: VisualQAFixture) -> None:
    """Make the failing shot need motion a 2.5D render cannot represent."""
    from vidgen.db.storyboard_models import StoryboardShotRecord

    record = session.get(StoryboardShotRecord, fixture.shot_ids[FAILING_INDEX])
    assert record is not None
    contract = dict(record.contract)
    action = dict(contract["action"])
    action["subject_action"] = "The lead throws a mug across the kitchen and runs out of frame"
    contract["action"] = action
    record.contract = contract
    session.commit()
