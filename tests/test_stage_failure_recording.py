"""Every stage that can stop a project records why it stopped.

Before this, only T11 episode analysis and T12 script generation ever wrote to
``pipeline_failure_events``. A project that died in narration, storyboard,
rendering or final QA was correctly reported as failed, but named no stage - so
the dashboard had nothing to offer a retry from, and the owner was back to
calling ``workflow:continue`` by hand.

These tests pin the three rules that make the table trustworthy: a stage that
stops records itself, a stage with its own richer failure keeps it, and a stage
that recovers clears its own row.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session

import vidgen.db  # noqa: F401  (registers every table on Base.metadata)
from vidgen.contracts.telemetry import FailureClass
from vidgen.db.base import Base
from vidgen.db.cost_models import PipelineFailureEvent
from vidgen.db.models import Project
from vidgen.telemetry.provider import record_pipeline_failure
from workers.temporal_worker.stage_failures import (
    record_stage_failure,
    render_failure_class,
    resolve_stage_failures,
)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'stages.db'}")
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def project_id(engine: Engine) -> UUID:
    identifier = uuid4()
    project = Project(
        id=identifier,
        name="Season 3 Episode 4",
        owner_subject="owner-a",
        status="narration",
        target_duration_seconds=300,
        visual_style="flat editorial cartoon",
        humor_intensity=6,
    )
    with Session(engine) as session:
        session.add(project)
        session.commit()
    return identifier


def _rows(engine: Engine, project_id: UUID) -> list[PipelineFailureEvent]:
    with Session(engine) as session:
        return list(
            session.scalars(
                select(PipelineFailureEvent).where(PipelineFailureEvent.project_id == project_id)
            )
        )


def test_a_stage_that_stops_records_the_stage_that_stopped(
    engine: Engine, project_id: UUID
) -> None:
    """Narration is the case the dashboard could not explain at all."""
    record_stage_failure(
        engine,
        project_id=project_id,
        stage="narration",
        idempotency_key="t18:narration:stage-failed",
        error=RuntimeError("the narration provider returned nothing usable"),
    )
    rows = _rows(engine, project_id)
    assert [row.stage for row in rows] == ["narration"]
    assert rows[0].resolved_at is None
    assert rows[0].projected_status == "narration_failed"


def test_a_stage_with_its_own_failure_keeps_it(engine: Engine, project_id: UUID) -> None:
    """The generic recorder fills a gap; it never overwrites a specific code.

    T12 names the validator that actually failed. A second, vaguer row would
    send the owner a worse explanation of the same stop.
    """
    with Session(engine) as session:
        record_pipeline_failure(
            session,
            project_id=project_id,
            workflow_id=f"vidgen-project-{project_id}",
            stage="script_generation",
            failure_class=FailureClass.CONTRACT_VALIDATION,
            error_code="COMPRESSION_VALIDATION_FAILED",
            retryable=False,
            projected_status="script_generation_failed",
            idempotency_key="t12:compress:exhausted",
        )
        session.commit()

    record_stage_failure(
        engine,
        project_id=project_id,
        stage="script_generation",
        idempotency_key="t18:script_generation:stage-failed",
        error=RuntimeError("COMPRESSION_VALIDATION_FAILED: attempts exhausted"),
    )
    rows = _rows(engine, project_id)
    assert [row.error_code for row in rows] == ["COMPRESSION_VALIDATION_FAILED"]


def test_a_stage_that_recovers_clears_its_own_row(engine: Engine, project_id: UUID) -> None:
    """Temporal retries activities; a stage that succeeded is not a blocker.

    Left open, the row would put a retry button on a project that is working.
    """
    record_stage_failure(
        engine,
        project_id=project_id,
        stage="storyboard",
        idempotency_key="t18:storyboard:stage-failed",
        error=TimeoutError("provider timed out"),
    )
    assert [row.resolved_at for row in _rows(engine, project_id)] == [None]

    resolve_stage_failures(engine, project_id=project_id, stage="storyboard")
    assert all(row.resolved_at is not None for row in _rows(engine, project_id))


def test_resolving_one_stage_leaves_another_stages_failure_open(
    engine: Engine, project_id: UUID
) -> None:
    """A recovered narration retry must not silence a dead storyboard."""
    for stage in ("narration", "storyboard"):
        record_stage_failure(
            engine,
            project_id=project_id,
            stage=stage,
            idempotency_key=f"t18:{stage}:stage-failed",
            error=RuntimeError("stopped"),
        )
    resolve_stage_failures(engine, project_id=project_id, stage="narration")
    open_stages = {row.stage for row in _rows(engine, project_id) if row.resolved_at is None}
    assert open_stages == {"storyboard"}


def test_recording_twice_writes_one_row(engine: Engine, project_id: UUID) -> None:
    """A Temporal retry that fails the same way is one failure, not two."""
    for _ in range(3):
        record_stage_failure(
            engine,
            project_id=project_id,
            stage="rendering",
            idempotency_key="t18:rendering:stage-failed",
            error=RuntimeError("render failed"),
        )
    assert len(_rows(engine, project_id)) == 1


def test_an_explicit_code_and_class_are_used_verbatim(engine: Engine, project_id: UUID) -> None:
    """Render and final QA report their outcome rather than raising it."""
    record_stage_failure(
        engine,
        project_id=project_id,
        stage="rendering",
        idempotency_key="t18:rendering:render-failed",
        error_code="CAPTION_DRIFT",
        failure_class=FailureClass.CONTRACT_VALIDATION,
        retryable=False,
    )
    row = _rows(engine, project_id)[0]
    assert row.error_code == "CAPTION_DRIFT"
    assert row.failure_class == FailureClass.CONTRACT_VALIDATION
    assert row.retryable is False


def test_bookkeeping_never_masks_the_failure_it_is_recording(project_id: UUID) -> None:
    """A recorder that raised would replace the real error with its own."""
    broken = create_engine("sqlite+pysqlite:///nonexistent-directory/none.db")
    # Must not raise: the caller is on its way to re-raising the real exception.
    record_stage_failure(
        broken,
        project_id=project_id,
        stage="narration",
        idempotency_key="t18:narration:stage-failed",
        error=RuntimeError("the original failure"),
    )
    resolve_stage_failures(broken, project_id=project_id, stage="narration")


def test_render_classifications_map_onto_the_telemetry_vocabulary() -> None:
    """A classification the dashboard cannot read is a failure it cannot explain."""
    assert render_failure_class("lineage") is FailureClass.CONTRACT_VALIDATION
    assert render_failure_class("transient") is FailureClass.TRANSPORT
    assert render_failure_class("cancelled") is FailureClass.CANCELLED
    assert render_failure_class("something-new") is FailureClass.UNKNOWN


def test_the_stage_wrapper_records_and_clears_around_the_real_handler(
    engine: Engine, project_id: UUID, tmp_path: Path
) -> None:
    """The wiring, not just the recorder: every linear stage funnels through here.

    ``_with_session`` is the one place that sees every T05-T13 stage activity, so
    it is what makes "any stage that stops names itself" true rather than
    aspirational. The handler is stubbed; the wrapper under test is the real one.
    """
    from apps.api.settings import APISettings
    from vidgen.contracts.workflow import StageActivityInput, StageActivityResult
    from workers.temporal_worker import production_handlers

    settings = APISettings(
        database_url=str(engine.url),
        blob_root=tmp_path / "blobs",
        upload_root=tmp_path / "uploads",
        signing_secret="test-secret",
    )
    request = StageActivityInput(
        project_id=project_id,
        source_video_id=uuid4(),
        stage="narration",
        idempotency_key="t18:narration",
    )

    def explode(*_: object) -> StageActivityResult:
        raise RuntimeError("the narration provider returned nothing usable")

    failing = production_handlers._with_session(settings, explode)
    with pytest.raises(RuntimeError, match="nothing usable"):
        failing(request)
    open_rows = [r for r in _rows(engine, project_id) if r.resolved_at is None]
    assert [r.stage for r in open_rows] == ["narration"]

    def succeed(*_: object) -> StageActivityResult:
        return StageActivityResult(stage="narration", entity_id=uuid4())

    production_handlers._with_session(settings, succeed)(request)
    assert [r for r in _rows(engine, project_id) if r.resolved_at is None] == []
