"""T22 Temporal orchestration, completion gating and T23 cost integration.

The workflow only ever exchanges compact, versioned, ID-only messages: reports,
findings, sampled frames, caption text, media bytes and provider payloads are
not representable in the contracts it passes, and the project cannot reach its
completed state without a current ``PASS``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from temporalio import activity
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from packages.workflows.activities import (
    configure_final_qa_handler,
    run_final_editorial_qa_activity,
)
from packages.workflows.project import ProjectWorkflow
from packages.workflows.shot import ProjectShotFanoutWorkflow, ShotWorkflow
from packages.workflows.shot_policy import TASK_QUEUE
from services.qa.final_commands import FinalQACommandOptions, run_final_editorial_qa
from tests.final_qa_fixtures import (
    FIXTURE_CONFIGURATION,
    FinalQAFixture,
    build_final_qa_project,
    require_ffmpeg,
)
from vidgen.contracts.shot_workflow import ProjectShotFanoutResult, ResolveShotFanoutResult
from vidgen.contracts.workflow import (
    FinalQAActivityInput,
    FinalQAActivityResult,
    ProjectWorkflowInput,
    ProjectWorkflowState,
    StageActivityInput,
    StageActivityResult,
)
from vidgen.db.base import Base
from vidgen.db.cost_models import (
    CostLedgerEntry,
    CostReservation,
    ProjectBudget,
    ProviderAttempt,
)
from vidgen.db.cost_repository import BudgetExceededError
from vidgen.db.final_editorial_models import (
    FinalEditorialProviderAttempt,
    FinalEditorialRun,
)
from vidgen.storage.blob import FilesystemBlobStore

PROJECT = UUID("00000000-0000-0000-0000-000000000001")
SOURCE = UUID("00000000-0000-0000-0000-000000000002")
RENDER_ASSET = UUID(int=5001)
MANIFEST_ASSET = UUID(int=5002)
RUN_ID = UUID(int=5003)
STORYBOARD_RUN = UUID(int=5004)


# --- ID-only message contracts ----------------------------------------------
def test_the_activity_message_carries_references_and_nothing_else() -> None:
    message = FinalQAActivityInput(
        project_id=PROJECT,
        final_render_asset_id=RENDER_ASSET,
        render_manifest_asset_id=MANIFEST_ASSET,
        final_editorial_run_id=RUN_ID,
        idempotency_key="project:t22",
        trace_context={"traceparent": "00-abc-def-01"},
    )
    assert set(message.model_dump()) == {
        "schema_version",
        "project_id",
        "final_render_asset_id",
        "render_manifest_asset_id",
        "final_editorial_run_id",
        "provider",
        "adjudicate",
        "idempotency_key",
        "trace_context",
    }


@pytest.mark.parametrize(
    "field",
    [
        "report",
        "findings",
        "captions",
        "frames",
        "script",
        "provider_payload",
        "media",
    ],
)
def test_the_activity_message_cannot_carry_a_payload(field: str) -> None:
    with pytest.raises(ValidationError):
        FinalQAActivityInput(
            project_id=PROJECT,
            idempotency_key="project:t22",
            **{field: "anything at all"},  # type: ignore[arg-type]
        )


def test_the_activity_result_carries_counts_and_a_decision_only() -> None:
    result = FinalQAActivityResult(
        project_id=PROJECT,
        final_editorial_run_id=RUN_ID,
        final_render_asset_id=RENDER_ASSET,
        status="FINAL_QA_PASSED",
        phase="COMPLETION_GATE",
        decision="PASS",
    )
    assert result.decision == "PASS"
    with pytest.raises(ValidationError):
        FinalQAActivityResult(
            project_id=PROJECT,
            final_editorial_run_id=RUN_ID,
            final_render_asset_id=RENDER_ASSET,
            status="FINAL_QA_PASSED",
            phase="COMPLETION_GATE",
            decision="MAYBE",  # type: ignore[arg-type]
        )
    with pytest.raises(ValidationError):
        FinalQAActivityResult(
            project_id=PROJECT,
            final_editorial_run_id=RUN_ID,
            final_render_asset_id=RENDER_ASSET,
            status="FINAL_QA_PASSED",
            phase="COMPLETION_GATE",
            findings=[{"summary": "leaked"}],  # type: ignore[call-arg]
        )


# --- the workflow ------------------------------------------------------------
def workflow_input() -> ProjectWorkflowInput:
    return ProjectWorkflowInput(
        project_id=PROJECT, source_video_id=SOURCE, idempotency_key="project-1"
    )


STAGES = (
    "upload",
    "media_processing",
    "transcript_acquisition",
    "evidence",
    "episode_analysis",
    "script_generation",
    "narration",
    "storyboard",
)


def stage_activities() -> list[Callable[..., Awaitable[object]]]:
    """One stub per T05-T13 stage, so the workflow reaches its T22 stage."""

    def make(stage: str) -> Callable[..., Awaitable[object]]:
        async def handler(request: StageActivityInput) -> StageActivityResult:
            return StageActivityResult(
                stage=request.stage,
                entity_id=STORYBOARD_RUN if request.stage == "storyboard" else None,
            )

        return activity.defn(name=f"run_{stage}_activity")(handler)

    return [make(stage) for stage in STAGES]


async def run_project(
    final_qa: Callable[..., Awaitable[FinalQAActivityResult]],
    *,
    shots: list[object] | None = None,
) -> ProjectWorkflowState:
    """Drive the real parent workflow with stubbed stages and one T22 stub."""

    async def resolve_fanout(request: object) -> ResolveShotFanoutResult:
        return ResolveShotFanoutResult(shots=list(shots or []))  # type: ignore[arg-type]

    async def fanout_checkpoint(value: ProjectShotFanoutResult) -> ProjectShotFanoutResult:
        return value

    activities: list[Callable[..., Awaitable[object]]] = [
        *stage_activities(),
        activity.defn(name="resolve_shot_fanout")(resolve_fanout),
        activity.defn(name="persist_shot_fanout_checkpoint")(fanout_checkpoint),
        activity.defn(name="run_final_editorial_qa_activity")(final_qa),
    ]
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as environment:
        async with Worker(
            environment.client,
            task_queue=TASK_QUEUE,
            workflows=[ProjectWorkflow, ProjectShotFanoutWorkflow, ShotWorkflow],
            activities=activities,
        ):
            handle = await environment.client.start_workflow(
                ProjectWorkflow.run,
                workflow_input(),
                id=f"project-{uuid4()}",
                task_queue=TASK_QUEUE,
            )
            return await handle.result()


def gate_activity(
    decision: str, status: str, seen: list[FinalQAActivityInput]
) -> Callable[..., Awaitable[FinalQAActivityResult]]:
    async def handler(request: FinalQAActivityInput) -> FinalQAActivityResult:
        seen.append(request)
        return FinalQAActivityResult(
            project_id=request.project_id,
            final_editorial_run_id=RUN_ID,
            final_render_asset_id=RENDER_ASSET,
            status=status,
            phase="COMPLETION_GATE",
            decision=decision,  # type: ignore[arg-type]
            blocking_finding_count=1 if decision == "FAIL" else 0,
            review_finding_count=1 if decision == "REVIEW" else 0,
        )

    return handler


@pytest.mark.asyncio
async def test_a_passing_gate_advances_the_project_to_completed() -> None:
    seen: list[FinalQAActivityInput] = []
    state = await run_project(gate_activity("PASS", "FINAL_QA_PASSED", seen))
    assert state.status == "completed"
    assert "final_editorial_qa" in state.completed_stages
    # The stage received IDs and a stable key, nothing else.
    assert len(seen) == 1
    assert seen[0].project_id == PROJECT
    assert seen[0].idempotency_key == "project-1:t22"
    assert seen[0].final_render_asset_id is None


@pytest.mark.asyncio
async def test_a_failed_gate_leaves_the_project_short_of_completed() -> None:
    seen: list[FinalQAActivityInput] = []
    state = await run_project(gate_activity("FAIL", "FINAL_QA_FAILED", seen))
    assert state.status == "FINAL_QA_FAILED"
    assert state.status != "completed"


@pytest.mark.asyncio
async def test_a_review_gate_leaves_the_project_short_of_completed() -> None:
    seen: list[FinalQAActivityInput] = []
    state = await run_project(gate_activity("REVIEW", "FINAL_QA_REVIEW_REQUIRED", seen))
    assert state.status == "FINAL_QA_REVIEW_REQUIRED"
    assert state.status != "completed"


@pytest.mark.asyncio
async def test_a_decision_of_pass_without_a_passing_status_does_not_complete() -> None:
    """The gate reads both fields, so neither one alone can advance the project."""
    seen: list[FinalQAActivityInput] = []
    state = await run_project(gate_activity("PASS", "FINAL_QA_REVIEW_REQUIRED", seen))
    assert state.status != "completed"


# --- T23 cost integration ----------------------------------------------------
pytestmark_ffmpeg = pytest.mark.skipif(
    not require_ffmpeg(), reason="FFmpeg and ffprobe are required"
)


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 't22-cost.db'}")
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture
def blob_root(tmp_path: Path) -> Path:
    root = tmp_path / "blobs"
    root.mkdir()
    return root


@pytest.fixture
def fixture(
    factory: sessionmaker[Session], blob_root: Path, tmp_path: Path
) -> Iterator[FinalQAFixture]:
    with factory() as session:
        yield build_final_qa_project(session, blob_root, tmp_path / "work")


def set_budget(session: Session, project_id: UUID, hard_cap: Decimal) -> None:
    budget = session.scalar(select(ProjectBudget).where(ProjectBudget.project_id == project_id))
    if budget is None:
        budget = ProjectBudget(
            project_id=project_id,
            warning_cap=hard_cap,
            hard_cap=hard_cap,
            currency="USD",
            policy_version="t23/1",
        )
        session.add(budget)
    else:
        budget.hard_cap = hard_cap
        budget.warning_cap = hard_cap
        budget.reserved_amount = Decimal("0")
        budget.committed_amount = Decimal("0")
        budget.released_amount = Decimal("0")
    session.commit()


def execute(session: Session, blob_root: Path, fixture: FinalQAFixture) -> object:
    store = FilesystemBlobStore(blob_root, b"test-secret")
    return asyncio.run(
        run_final_editorial_qa(
            session,
            store,
            project_id=fixture.project_id,
            options=FinalQACommandOptions(
                provider="fake",
                configuration=FIXTURE_CONFIGURATION,
                idempotency_key="t22-cost",
            ),
        )
    )


@pytestmark_ffmpeg
def test_the_editorial_call_reserves_and_reconciles_exactly_once(
    factory: sessionmaker[Session], blob_root: Path, fixture: FinalQAFixture
) -> None:
    with factory() as session:
        set_budget(session, fixture.project_id, Decimal("20.000000"))
        result = execute(session, blob_root, fixture)
        assert result.cost_microusd > 0  # type: ignore[attr-defined]

        attempts = session.scalars(select(FinalEditorialProviderAttempt)).all()
        assert len(attempts) == 1
        assert attempts[0].provider_attempt_id is not None
        assert attempts[0].status == "succeeded"

        provider_attempts = session.scalars(select(ProviderAttempt)).all()
        final_attempts = [
            row for row in provider_attempts if row.operation == "final_editorial_qa"
        ]
        assert len(final_attempts) == 1
        assert final_attempts[0].status == "SUCCEEDED"
        assert final_attempts[0].actual_cost > 0

        # Scope to T22: the fixture's own T20 runs reserve budget too.
        attempt_id = final_attempts[0].id
        reservations = session.scalars(
            select(CostReservation).where(CostReservation.provider_attempt_id == attempt_id)
        ).all()
        assert len(reservations) == 1
        entries = session.scalars(
            select(CostLedgerEntry).where(CostLedgerEntry.provider_attempt_id == attempt_id)
        ).all()
        assert entries


@pytestmark_ffmpeg
def test_an_idempotent_retry_creates_no_second_attempt_reservation_or_charge(
    factory: sessionmaker[Session], blob_root: Path, fixture: FinalQAFixture
) -> None:
    with factory() as session:
        set_budget(session, fixture.project_id, Decimal("20.000000"))
        first = execute(session, blob_root, fixture)
        attempts = len(session.scalars(select(ProviderAttempt)).all())
        reservations = len(session.scalars(select(CostReservation)).all())
        entries = len(session.scalars(select(CostLedgerEntry)).all())

        second = execute(session, blob_root, fixture)
        assert second.reused  # type: ignore[attr-defined]
        assert second.cost_microusd == first.cost_microusd  # type: ignore[attr-defined]
        # Not one more attempt, reservation, reconciliation or ledger entry.
        assert len(session.scalars(select(ProviderAttempt)).all()) == attempts
        assert len(session.scalars(select(CostReservation)).all()) == reservations
        assert len(session.scalars(select(CostLedgerEntry)).all()) == entries


@pytestmark_ffmpeg
def test_a_denied_budget_stops_the_run_without_bypassing_final_qa(
    factory: sessionmaker[Session], blob_root: Path, fixture: FinalQAFixture
) -> None:
    with factory() as session:
        # A hard cap below the estimated call cost: the reservation is denied.
        set_budget(session, fixture.project_id, Decimal("0.000001"))
        with pytest.raises(BudgetExceededError):
            execute(session, blob_root, fixture)

    with factory() as session:
        run = session.scalars(select(FinalEditorialRun)).one()
        # No decision was recorded, so nothing may complete on this render.
        assert run.final_decision is None
        assert not run.selected
        from services.qa.final_commands import completion_allowed

        allowed, reason = completion_allowed(
            session,
            project_id=fixture.project_id,
            final_render_asset_id=fixture.final_video_asset_id,
        )
        assert not allowed and reason == "final_qa_missing"


@pytestmark_ffmpeg
def test_a_provider_timeout_is_recorded_and_the_retry_reuses_the_attempt_identity(
    factory: sessionmaker[Session], blob_root: Path, fixture: FinalQAFixture
) -> None:
    """A failed attempt releases its reservation and does not bill the project."""
    from services.qa.final_editorial import FinalEditorialPipeline, FinalQAOptions
    from services.qa.final_fake_provider import FakeFinalEditorialProvider

    class TimingOutProvider(FakeFinalEditorialProvider):
        async def evaluate(self, call: object) -> object:  # type: ignore[override]
            raise TimeoutError("the editorial provider did not answer in time")

    store = FilesystemBlobStore(blob_root, b"test-secret")
    with factory() as session:
        set_budget(session, fixture.project_id, Decimal("20.000000"))
        pipeline = FinalEditorialPipeline(
            session,
            store,
            TimingOutProvider(),
            options=FinalQAOptions(configuration=FIXTURE_CONFIGURATION),
        )
        with pytest.raises(TimeoutError):
            asyncio.run(
                pipeline.evaluate_project(
                    project_id=fixture.project_id, idempotency_key="t22-timeout"
                )
            )

    with factory() as session:
        attempt = session.scalars(select(FinalEditorialProviderAttempt)).one()
        assert attempt.status == "failed"
        assert attempt.failure_class
        provider_attempt = session.scalars(
            select(ProviderAttempt).where(ProviderAttempt.operation == "final_editorial_qa")
        ).one()
        assert provider_attempt.status == "FAILED"
        assert provider_attempt.actual_cost == 0

        # The retry reuses the same durable attempt identity rather than
        # buying a second evaluation slot.
        retry = execute(session, blob_root, fixture)
        assert retry is not None
        assert (
            len(
                session.scalars(
                    select(ProviderAttempt).where(
                        ProviderAttempt.operation == "final_editorial_qa"
                    )
                ).all()
            )
            <= 2
        )


@pytestmark_ffmpeg
def test_no_paid_call_is_made_in_fake_mode(
    factory: sessionmaker[Session], blob_root: Path, fixture: FinalQAFixture
) -> None:
    with factory() as session:
        result = execute(session, blob_root, fixture)
        attempt = session.scalars(select(FinalEditorialProviderAttempt)).one()
        assert attempt.provider == "fake"
        assert result.first_pass_provider == "fake"  # type: ignore[attr-defined]


def test_the_activity_refuses_to_run_without_a_configured_handler() -> None:
    configure_final_qa_handler(None)
    with pytest.raises(RuntimeError, match="no activity handler configured"):
        run_final_editorial_qa_activity(
            FinalQAActivityInput(project_id=PROJECT, idempotency_key="k")
        )
