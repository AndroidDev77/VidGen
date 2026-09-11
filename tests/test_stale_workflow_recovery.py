"""Recovering a project whose Temporal workflow died without saying so.

A project workflow that fails inside an activity writes nothing back: the
``project_workflow_runs`` row keeps saying ``running`` and the project's
generation run keeps saying ``active``. The dashboard then draws a live
pipeline, and every ``workflow:continue`` is refused with
``project_generation_run_active`` - a project recoverable only by editing two
rows by hand.

These tests pin the way out: the API asks the cluster what actually happened,
settles the stale rows, and lets the retry through.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from services.control_plane.generation_runs import GenerationRunService
from services.control_plane.reconciliation import reconcile_project_workflow
from tests.review_fixtures import ProjectGraph, build_project_graph
from vidgen.contracts.control_commands import ProjectGenerationRunStatus
from vidgen.db.cost_models import PipelineFailureEvent
from vidgen.db.workflow_models import ProjectWorkflowRun
from vidgen.review.workflow_control import (
    EXECUTION_FAILED,
    EXECUTION_RUNNING,
    FakeWorkflowController,
    project_workflow_id,
)

OWNER = {"X-VidGen-User": "owner-a"}


def headers(key: str) -> dict[str, str]:
    return {**OWNER, "Idempotency-Key": key}


def api(project_id: UUID, suffix: str = "") -> str:
    return f"/api/v1/projects/{project_id}{suffix}"


@pytest.fixture
def graph(
    review_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
    tmp_path: Path,
) -> Iterator[ProjectGraph]:
    _, factory, _ = review_client
    with factory() as session:
        yield build_project_graph(session, owner_subject="owner-a", blob_root=tmp_path / "blobs")


def _start(client: TestClient, project_id: UUID) -> None:
    response = client.post(api(project_id, "/workflow:start"), json={}, headers=headers("start-1"))
    assert response.status_code == 200, response.text


def _kill_the_workflow(controller: FakeWorkflowController, project_id: UUID) -> str:
    """Make the cluster report a failed execution, as a dead workflow does.

    The workflow's own query is dropped as well: an execution that has already
    failed answers nothing, which is precisely why the database row is stale.
    """
    workflow_id = project_workflow_id(project_id)
    controller.execution_statuses[workflow_id] = EXECUTION_FAILED
    controller.states.pop(workflow_id, None)
    return workflow_id


def _record_failure(session: Session, project_id: UUID, *, stage: str) -> None:
    session.add(
        PipelineFailureEvent(
            project_id=project_id,
            workflow_id=f"vidgen-project-{project_id}",
            stage=stage,
            failure_class="contract_validation",
            error_code="COMPRESSION_VALIDATION_FAILED",
            retryable=False,
            event_version="pipeline.failure.v1",
            idempotency_key=f"t23:failure:{project_id}:stale",
            projected_status="script_generation_failed",
            created_at=datetime.now(UTC),
            diagnostics={},
        )
    )
    session.commit()


def test_continue_cancels_a_generation_run_whose_workflow_is_terminal(
    review_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
    graph: ProjectGraph,
) -> None:
    """The retry the owner presses must not need a hand-edited database.

    Before this, the run opened by ``workflow:start`` stayed active forever once
    its workflow died, and the dispatcher refused the continuation it blocked.
    """
    client, factory, controller = review_client
    _start(client, graph.project_id)
    with factory() as session:
        assert GenerationRunService(session).active(graph.project_id) is not None

    _kill_the_workflow(controller, graph.project_id)

    accepted = client.post(
        api(graph.project_id, "/workflow:continue"),
        json={"entry_stage": "script_generation", "reason": "remediation"},
        headers=headers("continue-1"),
    )
    assert accepted.status_code == 202, accepted.text

    with factory() as session:
        runs = GenerationRunService(session)
        # The stale run is settled, so the new command has a lineage to claim.
        stale = runs.history(graph.project_id)[0]
        assert stale.status == ProjectGenerationRunStatus.FAILED.value
        assert stale.active is False
        workflow_run = session.scalar(
            select(ProjectWorkflowRun).where(ProjectWorkflowRun.project_id == graph.project_id)
        )
        assert workflow_run is not None
        assert workflow_run.status == "failed"


def test_a_continuation_after_a_stale_run_actually_dispatches(
    review_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
    graph: ProjectGraph,
) -> None:
    """Accepting the command is only half of it: the dispatcher must start work."""
    from apps.api.settings import APISettings
    from services.control_plane.dispatcher import ControlCommandDispatcher
    from vidgen.db.control_command_models import ControlCommandRecord

    client, factory, controller = review_client
    _start(client, graph.project_id)
    _kill_the_workflow(controller, graph.project_id)

    command = client.post(
        api(graph.project_id, "/workflow:continue"),
        json={"entry_stage": "script_generation", "reason": "remediation"},
        headers=headers("continue-1"),
    ).json()["command"]

    settings = APISettings()
    ControlCommandDispatcher(
        factory,
        controller,
        dispatcher_id="test-dispatcher",
        image_provider_name=settings.image_provider_name,
        image_model=settings.image_model,
        video_provider_name=settings.video_provider_name,
        visual_capability_profile=settings.visual_capability_profile,
    ).run_once()

    with factory() as session:
        record = session.get(ControlCommandRecord, UUID(command["command_id"]))
        assert record is not None
        # The stale lineage used to fail this with ``project_generation_run_active``.
        assert record.error_code is None
        assert record.workflow_id is not None


def test_a_live_workflow_is_left_alone(
    review_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
    graph: ProjectGraph,
) -> None:
    """Reconciliation must never cancel work that is still running."""
    client, factory, controller = review_client
    _start(client, graph.project_id)
    controller.execution_statuses[project_workflow_id(graph.project_id)] = EXECUTION_RUNNING

    with factory() as session:
        outcome = reconcile_project_workflow(
            session, project_id=graph.project_id, controller=controller
        )
        assert outcome.changed is False
        assert GenerationRunService(session).active(graph.project_id) is not None


def test_an_unreachable_cluster_is_not_evidence_of_a_failure(
    review_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
    graph: ProjectGraph,
) -> None:
    """A controller that cannot answer must leave the project exactly as it was."""
    client, factory, controller = review_client
    _start(client, graph.project_id)
    controller.missing_workflows.add(project_workflow_id(graph.project_id))
    controller.states.pop(project_workflow_id(graph.project_id), None)

    with factory() as session:
        outcome = reconcile_project_workflow(
            session, project_id=graph.project_id, controller=controller
        )
        assert outcome.execution_status is None
        assert outcome.changed is False
        assert GenerationRunService(session).active(graph.project_id) is not None


def test_the_status_endpoint_reports_the_failure_the_workflow_never_wrote(
    review_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
    graph: ProjectGraph,
) -> None:
    """The dashboard polls this endpoint; it is where the truth has to surface.

    The workflow answers no query once it has failed, so without reconciliation
    the projection falls back to the row - ``running`` - and the owner watches a
    pipeline that stopped hours ago.
    """
    client, factory, controller = review_client
    _start(client, graph.project_id)
    _kill_the_workflow(controller, graph.project_id)
    with factory() as session:
        _record_failure(session, graph.project_id, stage="script_generation")

    body = client.get(api(graph.project_id, "/workflow"), headers=OWNER).json()
    assert body["status"] == "failed"
    # The stage that failed is named, so the timeline has something to retry.
    failed = [stage for stage in body["stages"] if stage["state"] == "failed"]
    assert [stage["stage"] for stage in failed] == ["script_generation"]


def test_reconciliation_is_idempotent(
    review_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
    graph: ProjectGraph,
) -> None:
    """A second pass over a settled run changes nothing and settles nothing."""
    client, factory, controller = review_client
    _start(client, graph.project_id)
    _kill_the_workflow(controller, graph.project_id)

    with factory() as session:
        first = reconcile_project_workflow(
            session, project_id=graph.project_id, controller=controller
        )
        session.commit()
    assert first.run_status == "failed"
    assert first.settled_generation_run is True

    with factory() as session:
        second = reconcile_project_workflow(
            session, project_id=graph.project_id, controller=controller
        )
        assert second.changed is False
