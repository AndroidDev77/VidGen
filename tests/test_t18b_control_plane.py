"""T18b: every accepted asynchronous command is durable, dispatched and truthful.

These tests exercise the invariant the whole task exists for: an API that
answers ``202 Accepted`` has already written a command row a dispatcher can
claim, and a command only reports itself ``running`` once a real workflow has
been started and its identity persisted.

Everything runs against SQLite with the deterministic fake workflow controller.
No Temporal cluster and no paid provider call is involved.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from apps.api.settings import APISettings
from services.control_plane.commands import ControlPlaneService, request_digest
from services.control_plane.dispatcher import ControlCommandDispatcher
from services.control_plane.generation_runs import GenerationRunService
from services.control_plane.references import reference_run_id, resolve_reference_inputs
from services.control_plane.revisions import plan_revision
from tests.review_fixtures import ProjectGraph, build_project_graph
from vidgen.contracts.control_commands import (
    ControlCommandFailure,
    ControlCommandRequest,
    ControlCommandStatus,
    ControlCommandTargetType,
    ControlCommandType,
)
from vidgen.contracts.workflow import FinalQAActivityResult, RenderActivityResult
from vidgen.db.control_command_models import ControlCommandRecord
from vidgen.db.control_command_repository import (
    ControlCommandError,
    ControlCommandRepository,
)
from vidgen.db.models import Project
from vidgen.review.workflow_control import FakeWorkflowController

OWNER = {"X-VidGen-User": "owner-a"}
IDENTITY = "a" * 64


def headers(*, if_match: int | None = None, key: str | None = None) -> dict[str, str]:
    out = dict(OWNER)
    if if_match is not None:
        out["If-Match"] = str(if_match)
    if key is not None:
        out["Idempotency-Key"] = key
    return out


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


@pytest.fixture
def client(
    review_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
) -> TestClient:
    return review_client[0]


@pytest.fixture
def controller(
    review_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
) -> FakeWorkflowController:
    return review_client[2]


@pytest.fixture
def dispatcher(
    review_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
) -> ControlCommandDispatcher:
    """A dispatcher over the same database and controller the API is using."""
    _, factory, workflow_controller = review_client
    # The same provider configuration the API composes shot identities from.
    # A dispatcher configured differently would derive a different identity and
    # correctly refuse the command as stale - which is the behaviour under test
    # elsewhere, not here.
    settings = APISettings()
    return ControlCommandDispatcher(
        factory,
        workflow_controller,
        dispatcher_id="test-dispatcher",
        image_provider_name=settings.image_provider_name,
        image_model=settings.image_model,
        video_provider_name=settings.video_provider_name,
        visual_capability_profile=settings.visual_capability_profile,
    )


def _request(project_id: UUID, key: str = "k1", payload: object = None) -> ControlCommandRequest:
    return ControlCommandRequest(
        project_id=project_id,
        owner_subject="owner-a",
        command_type=ControlCommandType.FINAL_QA_RUN,
        target_type=ControlCommandTargetType.PROJECT,
        target_id=project_id,
        idempotency_key=key,
        request_hash=request_digest(payload or {"a": 1}),
        upstream_input_identity=IDENTITY,
    )


def _commands(factory: sessionmaker[Session], project_id: UUID) -> list[ControlCommandRecord]:
    with factory() as session:
        return list(
            session.scalars(
                select(ControlCommandRecord)
                .where(ControlCommandRecord.project_id == project_id)
                .order_by(ControlCommandRecord.created_at)
            )
        )


# ---------------------------------------------------------------------------
# Durable commands
# ---------------------------------------------------------------------------


def test_a_command_is_created_once_and_replayed_thereafter(
    review_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
    graph: ProjectGraph,
) -> None:
    _, factory, _ = review_client
    with factory() as session:
        repository = ControlCommandRepository(session)
        first = repository.create(_request(graph.project_id))
        session.commit()
        second = repository.create(_request(graph.project_id))
        session.commit()
    assert first.created is True
    assert second.created is False
    assert first.record.id == second.record.id
    assert len(_commands(factory, graph.project_id)) == 1


def test_the_same_key_with_different_material_is_rejected(
    review_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
    graph: ProjectGraph,
) -> None:
    """Idempotency binds a key to its request; it never re-dispatches a new one."""
    _, factory, _ = review_client
    with factory() as session:
        repository = ControlCommandRepository(session)
        repository.create(_request(graph.project_id, payload={"a": 1}))
        session.commit()
        with pytest.raises(ControlCommandError) as failure:
            repository.create(_request(graph.project_id, payload={"a": 2}))
    assert failure.value.code == "command_idempotency_mismatch"


def test_two_dispatchers_cannot_claim_the_same_command(
    review_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
    graph: ProjectGraph,
) -> None:
    """The claim is a guarded UPDATE, so exactly one dispatcher can win."""
    _, factory, _ = review_client
    with factory() as session:
        record = ControlCommandRepository(session).create(_request(graph.project_id)).record
        session.commit()
        command_id = record.id
    with factory() as first_session, factory() as second_session:
        first = ControlCommandRepository(first_session)
        second = ControlCommandRepository(second_session)
        first_record = first.get(graph.project_id, command_id)
        second_record = second.get(graph.project_id, command_id)
        assert first_record is not None and second_record is not None
        assert first.claim(first_record, claim_owner="dispatcher-a") is True
        first_session.commit()
        assert second.claim(second_record, claim_owner="dispatcher-b") is False
        second_session.commit()
    with factory() as session:
        settled = session.get(ControlCommandRecord, command_id)
        assert settled is not None
        assert settled.claim_owner == "dispatcher-a"
        assert settled.attempt == 1


def test_an_expired_lease_is_recovered_by_the_next_dispatcher(
    review_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
    graph: ProjectGraph,
) -> None:
    """A killed dispatcher must not strand a command forever."""
    _, factory, _ = review_client
    with factory() as session:
        repository = ControlCommandRepository(session)
        record = repository.create(_request(graph.project_id)).record
        repository.claim(record, claim_owner="dispatcher-a", lease_seconds=60)
        session.commit()
        command_id = record.id
    with factory() as session:
        stale = session.get(ControlCommandRecord, command_id)
        assert stale is not None
        stale.lease_expires_at = datetime.now(UTC) - timedelta(minutes=5)
        session.commit()
    with factory() as session:
        repository = ControlCommandRepository(session)
        claimable = repository.claimable(limit=10)
        assert [item.id for item in claimable] == [command_id]
        assert repository.claim(claimable[0], claim_owner="dispatcher-b") is True
        session.commit()
    with factory() as session:
        recovered = session.get(ControlCommandRecord, command_id)
        assert recovered is not None
        assert recovered.claim_owner == "dispatcher-b"
        assert recovered.attempt == 2


def test_a_command_is_retried_within_its_bound_and_then_fails(
    review_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
    graph: ProjectGraph,
) -> None:
    _, factory, _ = review_client
    with factory() as session:
        repository = ControlCommandRepository(session)
        record = repository.create(_request(graph.project_id)).record
        record.max_attempts = 2
        session.flush()
        repository.claim(record, claim_owner="d")
        repository.fail(
            record,
            ControlCommandFailure(code="transient", summary="try again", retryable=True),
        )
        session.commit()
        assert record.status == ControlCommandStatus.PENDING.value
        repository.claim(record, claim_owner="d")
        repository.fail(
            record,
            ControlCommandFailure(code="transient", summary="try again", retryable=True),
        )
        session.commit()
        command_id = record.id
    with factory() as session:
        settled = session.get(ControlCommandRecord, command_id)
        assert settled is not None
        assert settled.status == ControlCommandStatus.FAILED.value
        assert settled.error_code == "transient"


def test_a_running_command_cannot_exist_without_a_workflow_identity(
    review_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
    graph: ProjectGraph,
) -> None:
    """The database itself refuses a calculated-but-never-started workflow ID."""
    from sqlalchemy.exc import IntegrityError

    _, factory, _ = review_client
    with factory() as session:
        record = ControlCommandRepository(session).create(_request(graph.project_id)).record
        session.commit()
        record.status = ControlCommandStatus.RUNNING.value
        record.workflow_id = None
        with pytest.raises(IntegrityError):
            session.commit()


def test_the_repository_refuses_to_run_a_command_without_a_workflow(
    review_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
    graph: ProjectGraph,
) -> None:
    _, factory, _ = review_client
    with factory() as session:
        repository = ControlCommandRepository(session)
        record = repository.create(_request(graph.project_id)).record
        repository.claim(record, claim_owner="d")
        repository.mark_dispatching(record)
        with pytest.raises(ControlCommandError) as failure:
            repository.mark_running(record, workflow_id="", run_id=None)
    assert failure.value.code == "command_dispatch_identity_missing"


def test_an_owner_can_cancel_and_retry_a_command_through_the_api(
    client: TestClient, graph: ProjectGraph
) -> None:
    created = client.post(
        api(graph.project_id, "/final-qa:run"),
        json={"provider": "fake", "adjudicate": False},
        headers=headers(if_match=1, key="final-qa-1"),
    )
    assert created.status_code == 202
    command_id = created.json()["command_id"]

    listed = client.get(api(graph.project_id, "/commands"), headers=OWNER).json()
    assert [item["command_id"] for item in listed["items"]] == [command_id]
    assert listed["items"][0]["permitted_actions"] == ["cancel"]

    cancelled = client.post(
        api(graph.project_id, f"/commands/{command_id}:cancel"), headers=OWNER
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["command"]["status"] == "cancelled"
    assert cancelled.json()["command"]["permitted_actions"] == []


def test_a_command_is_not_visible_to_another_owner(
    client: TestClient, graph: ProjectGraph
) -> None:
    created = client.post(
        api(graph.project_id, "/final-qa:run"),
        json={"provider": "fake", "adjudicate": False},
        headers=headers(if_match=1, key="final-qa-1"),
    ).json()
    response = client.get(
        api(graph.project_id, f"/commands/{created['command_id']}"),
        headers={"X-VidGen-User": "owner-b"},
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def test_dispatching_a_final_qa_command_starts_a_real_workflow(
    client: TestClient,
    graph: ProjectGraph,
    dispatcher: ControlCommandDispatcher,
    controller: FakeWorkflowController,
    review_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
) -> None:
    _, factory, _ = review_client
    created = client.post(
        api(graph.project_id, "/final-qa:run"),
        json={"provider": "fake", "adjudicate": False},
        headers=headers(if_match=1, key="final-qa-1"),
    ).json()
    assert created["workflow_id"] is None, "nothing has been started yet"

    report = dispatcher.run_once()
    assert (report.claimed, report.dispatched) == (1, 1)

    command = client.get(
        api(graph.project_id, f"/commands/{created['command_id']}"), headers=OWNER
    ).json()["command"]
    assert command["status"] == "running"
    assert command["workflow_id"] in controller.final_qa
    assert controller.final_qa[command["workflow_id"]].project_id == graph.project_id
    with factory() as session:
        stored = session.get(ControlCommandRecord, UUID(created["command_id"]))
        assert stored is not None and stored.workflow_id == command["workflow_id"]


def test_a_dispatched_command_settles_from_the_workflows_own_result(
    client: TestClient,
    graph: ProjectGraph,
    dispatcher: ControlCommandDispatcher,
    controller: FakeWorkflowController,
) -> None:
    created = client.post(
        api(graph.project_id, "/final-qa:run"),
        json={"provider": "fake", "adjudicate": False},
        headers=headers(if_match=1, key="final-qa-1"),
    ).json()
    dispatcher.run_once()
    workflow_id = next(iter(controller.final_qa))
    controller.final_qa_states[workflow_id] = FinalQAActivityResult(
        project_id=graph.project_id,
        final_editorial_run_id=uuid4(),
        final_render_asset_id=graph.final_video_asset_id or uuid4(),
        status="FINAL_QA_PASSED",
        phase="complete",
        decision="PASS",
    )
    dispatcher.settle_running()
    command = client.get(
        api(graph.project_id, f"/commands/{created['command_id']}"), headers=OWNER
    ).json()["command"]
    assert command["status"] == "completed"
    assert command["result"]["summary"]["decision"] == "PASS"
    assert command["progress"]["percent"] == 100


def test_no_command_is_left_pending_after_a_dispatch_pass(
    client: TestClient,
    graph: ProjectGraph,
    dispatcher: ControlCommandDispatcher,
    review_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
) -> None:
    """Whatever happens, an accepted command must not stay silently queued."""
    _, factory, _ = review_client
    shot = client.get(api(graph.project_id, f"/shots/{graph.shot_ids[4]}"), headers=OWNER).json()
    client.post(
        api(graph.project_id, f"/shots/{graph.shot_ids[4]}:regenerate"),
        json={"confirm_invalidation": True},
        headers=headers(if_match=shot["shot"]["row_version"], key="regen-1"),
    )
    client.post(
        api(graph.project_id, "/final-qa:run"),
        json={"provider": "fake", "adjudicate": False},
        headers=headers(if_match=1, key="final-qa-1"),
    )
    dispatcher.run_once()
    statuses = {record.status for record in _commands(factory, graph.project_id)}
    assert ControlCommandStatus.PENDING.value not in statuses


def test_a_dispatch_whose_upstream_moved_fails_with_an_actionable_code(
    client: TestClient,
    graph: ProjectGraph,
    dispatcher: ControlCommandDispatcher,
    review_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
) -> None:
    """A stale command must not spend money producing something nobody asked for."""
    _, factory, _ = review_client
    created = client.post(
        api(graph.project_id, "/final-qa:run"),
        json={"provider": "fake", "adjudicate": False},
        headers=headers(if_match=1, key="final-qa-1"),
    ).json()
    with factory() as session:
        record = session.get(ControlCommandRecord, UUID(created["command_id"]))
        assert record is not None
        record.upstream_input_identity = "b" * 64
        session.commit()
    dispatcher.run_once()
    command = client.get(
        api(graph.project_id, f"/commands/{created['command_id']}"), headers=OWNER
    ).json()["command"]
    assert command["status"] == "failed"
    assert command["failure"]["code"] == "command_upstream_stale"


# ---------------------------------------------------------------------------
# Voice profiles
# ---------------------------------------------------------------------------


def test_a_project_can_be_created_with_a_voice_and_started(
    client: TestClient,
    review_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
) -> None:
    catalog_project = client.post(
        "/api/v1/projects", json={"name": "voice-first"}, headers=OWNER
    ).json()
    assert catalog_project["voice_profile_id"] is None
    catalog = client.get(
        f"/api/v1/projects/{catalog_project['id']}/voice-profiles", headers=OWNER
    ).json()
    fake = next(item for item in catalog["items"] if item["provider"] == "fake")
    assert fake["scope"] == "shared" and fake["selected"] is False

    created = client.post(
        "/api/v1/projects",
        json={"name": "with-voice", "voice_profile_id": fake["voice_profile_id"]},
        headers=OWNER,
    ).json()
    assert created["voice_profile_id"] is not None
    selection = client.get(
        f"/api/v1/projects/{created['id']}/voice-profile", headers=OWNER
    ).json()["profile"]
    assert selection["provider"] == "fake"
    assert selection["selected"] is True
    # Never a credential, and never a provider secret of any kind.
    assert not any("key" in name or "secret" in name for name in selection)


def test_a_voice_profile_from_another_project_is_refused(
    client: TestClient,
    graph: ProjectGraph,
    review_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
    tmp_path: Path,
) -> None:
    """A project-scoped profile is exactly that, for the owner and for anyone else."""
    _, factory, _ = review_client
    with factory() as session:
        other = build_project_graph(
            session, owner_subject="owner-a", name="Other", blob_root=tmp_path / "other"
        )
        foreign_voice = session.get(Project, other.project_id)
        assert foreign_voice is not None
        foreign_id = foreign_voice.settings["voice_profile_id"]
    response = client.put(
        api(graph.project_id, "/voice-profile"),
        json={"voice_profile_id": foreign_id},
        headers=OWNER,
    )
    assert response.status_code == 404


def test_an_unconfigured_provider_cannot_be_selected(
    client: TestClient, graph: ProjectGraph
) -> None:
    response = client.put(
        api(graph.project_id, "/voice-profile"),
        json={"provider": "elevenlabs", "provider_voice_id": "rachel"},
        headers=OWNER,
    )
    assert response.status_code == 409
    assert "not configured" in response.json()["summary"]


def test_changing_the_voice_changes_the_narration_identity(
    client: TestClient, graph: ProjectGraph
) -> None:
    """A different voice is different material, so T12 must not reuse the old run."""
    before = client.get(api(graph.project_id, "/voice-profile"), headers=OWNER).json()["profile"]
    updated = client.put(
        api(graph.project_id, "/voice-profile"),
        json={"provider": "fake", "provider_voice_id": "another-local-voice"},
        headers=OWNER,
    ).json()["profile"]
    assert updated["voice_profile_id"] != before["voice_profile_id"]
    assert updated["configuration_hash"] != "" and updated["provider_voice_id"] != (
        before["provider_voice_id"]
    )


# ---------------------------------------------------------------------------
# T19
# ---------------------------------------------------------------------------


def test_the_worker_registers_the_continuity_workflow_and_its_activities() -> None:
    """The T19 workflow named two activities that nothing defined until T18b."""
    from workers.temporal_worker.registry import ACTIVITIES, WORKFLOWS

    names = {
        getattr(activity, "__temporal_activity_definition").name for activity in ACTIVITIES
    }
    assert {
        "resolve_continuity_inputs",
        "build_continuity_references",
        "apply_continuity_references",
    } <= names
    assert "ContinuityReferenceWorkflow" in {workflow.__name__ for workflow in WORKFLOWS}
    assert {"FinalEditorialQAWorkflow", "RenderWorkflow"} <= {
        workflow.__name__ for workflow in WORKFLOWS
    }


def test_a_reference_build_dispatches_the_real_t19_workflow(
    client: TestClient,
    graph: ProjectGraph,
    dispatcher: ControlCommandDispatcher,
    controller: FakeWorkflowController,
    review_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
) -> None:
    _, factory, _ = review_client
    queued = client.post(
        api(graph.project_id, "/references:build"),
        json={"provider": "fake", "model": "fake-v1"},
        headers=headers(if_match=1, key="build-1"),
    )
    assert queued.status_code == 202
    body = queued.json()
    assert body["command_id"] and body["workflow_id"] is None

    dispatcher.run_once()
    command = client.get(
        api(graph.project_id, f"/commands/{body['command_id']}"), headers=OWNER
    ).json()["command"]
    with factory() as session:
        expected = resolve_reference_inputs(
            session, project_id=graph.project_id, idempotency_key="probe"
        )
    assert command["status"] in {"running", "awaiting_review"}
    assert command["workflow_id"] == f"vidgen-references-{expected.reference_run_id}"
    assert controller.references[command["workflow_id"]].storyboard_run_id == (
        expected.storyboard_run_id
    )


def test_the_reference_run_is_derived_from_the_authoritative_inputs(
    review_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
    graph: ProjectGraph,
) -> None:
    """Everyone that addresses a reference run must derive the same ID."""
    _, factory, _ = review_client
    with factory() as session:
        request = resolve_reference_inputs(
            session, project_id=graph.project_id, idempotency_key="probe"
        )
    assert request.reference_run_id == reference_run_id(
        episode_analysis_id=request.episode_analysis_id,
        storyboard_run_id=request.storyboard_run_id,
    )


def test_an_approval_signals_the_waiting_reference_workflow(
    client: TestClient,
    graph: ProjectGraph,
    dispatcher: ControlCommandDispatcher,
    controller: FakeWorkflowController,
    review_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
) -> None:
    """An approval must reach the workflow, not just update a row."""
    _, factory, _ = review_client
    reference_set_id, entity_id = _seed_reference_set(factory, graph)
    approved = client.post(
        api(
            graph.project_id,
            f"/characters/{entity_id}/references/{reference_set_id}:approve",
        ),
        json={"upstream_lineage_hash": IDENTITY, "confirm_invalidation": True},
        headers=headers(if_match=1, key="approve-1"),
    )
    assert approved.status_code == 200
    assert approved.json()["command_id"]

    dispatcher.run_once()
    assert controller.reference_approvals, "no approval reached the T19 workflow"
    workflow_id, signal = controller.reference_approvals[0]
    with factory() as session:
        expected = resolve_reference_inputs(
            session, project_id=graph.project_id, idempotency_key="probe"
        )
    assert workflow_id == f"vidgen-references-{expected.reference_run_id}"
    assert signal.approved_reference_set_ids == [reference_set_id]


def _seed_reference_set(
    factory: sessionmaker[Session], graph: ProjectGraph
) -> tuple[UUID, UUID]:
    """Persist one drafted character reference set for the project."""
    from sqlalchemy import insert

    from vidgen.db.continuity_models import (
        character_identity_versions,
        character_reference_sets,
    )
    from vidgen.db.episode_analysis_models import EpisodeAnalysisRecord
    from vidgen.db.models import Character

    now = datetime.now(UTC)
    with factory() as session:
        analysis = session.scalar(
            select(EpisodeAnalysisRecord).where(
                EpisodeAnalysisRecord.project_id == graph.project_id
            )
        )
        assert analysis is not None
        character = Character(
            project_id=graph.project_id, canonical_name="Mira", definition={"hair": "black"}
        )
        session.add(character)
        session.flush()
        identity_version_id = uuid4()
        session.execute(
            insert(character_identity_versions).values(
                id=identity_version_id,
                project_id=graph.project_id,
                character_id=character.id,
                episode_analysis_id=analysis.id,
                version=1,
                identity={"display_name": "Mira"},
                identity_hash="c" * 64,
                status="draft",
                created_at=now,
                updated_at=now,
            )
        )
        reference_set_id = uuid4()
        session.execute(
            insert(character_reference_sets).values(
                id=reference_set_id,
                project_id=graph.project_id,
                identity_version_id=identity_version_id,
                reference_identity="d" * 64,
                status="draft",
                ordered_asset_ids=[],
                validation_report={"valid": True},
                row_version=1,
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()
        return reference_set_id, character.id


def test_a_project_with_no_reference_evidence_needs_no_approval(
    review_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
    graph: ProjectGraph,
) -> None:
    """T19 must complete deterministically rather than wait forever."""
    from services.continuity.orchestrator import (
        ContinuityReferenceOrchestrator,
        resolve_requirements,
    )

    _, factory, _ = review_client
    with factory() as session:
        assert resolve_requirements(session, graph.project_id) == []
        outcome = ContinuityReferenceOrchestrator(
            session,
            generator=lambda *_: pytest.fail("no sheet may be generated without evidence"),
            provider="fake",
            model="fake",
        ).build(
            project_id=graph.project_id,
            episode_analysis_id=uuid4(),
            reference_run_id=uuid4(),
            idempotency_key="build",
        )
    assert outcome.requires_approval is False
    assert outcome.draft_reference_set_ids == ()


# ---------------------------------------------------------------------------
# Shot regeneration
# ---------------------------------------------------------------------------


def test_regeneration_starts_a_real_replacement_child_workflow(
    client: TestClient,
    graph: ProjectGraph,
    dispatcher: ControlCommandDispatcher,
    controller: FakeWorkflowController,
) -> None:
    target = graph.shot_ids[3]
    shot = client.get(api(graph.project_id, f"/shots/{target}"), headers=OWNER).json()
    created = client.post(
        api(graph.project_id, f"/shots/{target}:regenerate"),
        json={"confirm_invalidation": True},
        headers=headers(if_match=shot["shot"]["row_version"], key="regen-1"),
    ).json()
    assert created["child_workflow_id"] is None

    dispatcher.run_once()
    assert controller.shot_start_calls == 1
    started = next(iter(controller.shots.values()))
    assert started.workflow_identity.regeneration_sequence == 1
    assert started.shot_input_hash == created["new_identity_hash"]

    command = client.get(
        api(graph.project_id, f"/commands/{created['command_id']}"), headers=OWNER
    ).json()["command"]
    assert command["status"] == "running"
    assert command["workflow_id"] == next(iter(controller.shots))


def test_a_duplicated_regeneration_starts_no_second_child(
    client: TestClient,
    graph: ProjectGraph,
    dispatcher: ControlCommandDispatcher,
    controller: FakeWorkflowController,
) -> None:
    target = graph.shot_ids[3]
    shot = client.get(api(graph.project_id, f"/shots/{target}"), headers=OWNER).json()
    for _ in range(2):
        client.post(
            api(graph.project_id, f"/shots/{target}:regenerate"),
            json={"confirm_invalidation": True},
            headers=headers(if_match=shot["shot"]["row_version"], key="regen-1"),
        )
    dispatcher.run_once()
    dispatcher.run_once()
    assert controller.shot_start_calls == 1
    assert len(controller.shots) == 1


# ---------------------------------------------------------------------------
# T22 and remediation
# ---------------------------------------------------------------------------


def test_a_routing_only_remediation_target_is_refused_rather_than_accepted(
    client: TestClient,
    graph: ProjectGraph,
    review_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
) -> None:
    """A classification with no executable stage must not answer 202."""
    run_id = _seed_final_qa_run(review_client[1], graph)
    response = client.post(
        api(graph.project_id, f"/final-qa/{run_id}:remediate"),
        json={"target": "HUMAN_EDITORIAL_REVIEW", "finding_ids": [str(uuid4())]},
        headers=headers(if_match=1, key="remediate-1"),
    )
    assert response.status_code == 409
    assert response.json()["code"] == "remediation_unsupported"


def _seed_final_qa_run(factory: sessionmaker[Session], graph: ProjectGraph) -> UUID:
    """One completed T22 run bound to the fixture's render, with no findings."""
    from vidgen.db.final_editorial_models import FinalEditorialRun
    from vidgen.db.models import RenderJob

    with factory() as session:
        render = session.scalar(select(RenderJob).where(RenderJob.project_id == graph.project_id))
        assert render is not None and render.final_video_asset_id is not None
        assert render.manifest_asset_id is not None
        run = FinalEditorialRun(
            project_id=graph.project_id,
            render_job_id=render.id,
            final_render_asset_id=render.final_video_asset_id,
            render_manifest_asset_id=render.manifest_asset_id,
            render_identity=render.render_identity or IDENTITY,
            final_qa_identity="e" * 64,
            input_hash=IDENTITY,
            configuration_hash=IDENTITY,
            idempotency_key="t22:1",
            status="FINAL_QA_PASSED",
            current_phase="COMPLETION_GATE",
            final_decision="PASS",
            first_pass_provider="fake",
            pipeline_version="t22/1",
            gate_version="final-gate/1",
            selected=False,
        )
        session.add(run)
        session.commit()
        return run.id


def test_manual_final_qa_requires_a_completed_render(
    client: TestClient,
    graph: ProjectGraph,
    review_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
) -> None:
    _, factory, _ = review_client
    from vidgen.db.models import RenderJob

    with factory() as session:
        render = session.scalar(
            select(RenderJob).where(RenderJob.project_id == graph.project_id)
        )
        assert render is not None
        render.status = "render_failed"
        session.commit()
    response = client.post(
        api(graph.project_id, "/final-qa:run"),
        json={"provider": "fake", "adjudicate": False},
        headers=headers(if_match=1, key="final-qa-1"),
    )
    assert response.status_code == 409
    assert response.json()["code"] == "render_not_verified"


# ---------------------------------------------------------------------------
# Revisions and continuation
# ---------------------------------------------------------------------------


def test_a_transcript_edit_starts_the_rebuild_it_invalidated(
    client: TestClient,
    graph: ProjectGraph,
    review_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
) -> None:
    _, factory, _ = review_client
    transcript = client.get(api(graph.project_id, "/transcript"), headers=OWNER).json()
    segment = transcript["segments"][0]
    response = client.patch(
        api(graph.project_id, f"/transcript/segments/{segment['segment_id']}"),
        json={"text": "a corrected line", "confirm_invalidation": True},
        headers=headers(if_match=segment["row_version"], key="edit-1"),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["rebuild_command_id"]
    # The transcript is upstream of every generated stage, so the rebuild starts
    # at the episode analysis; the source media and the transcript are reused.
    assert body["rebuild_entry_stage"] == "episode_analysis"
    with factory() as session:
        plan = plan_revision(
            session,
            project_id=graph.project_id,
            kind="transcript",
            source_id=graph.transcript_id,
        )
    assert "upload" in plan.reused_stages and "narration" in plan.rebuilt_stages


def test_selecting_a_script_version_starts_the_rebuild_from_narration(
    client: TestClient, graph: ProjectGraph
) -> None:
    """Narration is the first stage that would spend money on the new script."""
    scripts = client.get(api(graph.project_id, "/scripts"), headers=OWNER).json()["items"]
    target = scripts[0]
    response = client.post(
        api(graph.project_id, f"/scripts/{target['script_id']}:select"),
        headers=headers(if_match=target["row_version"], key="select-1"),
    )
    assert response.status_code == 200
    assert response.json()["rebuild_entry_stage"] == "narration"
    assert response.json()["rebuild_command_id"]


def test_continuing_a_project_opens_a_new_generation_run(
    client: TestClient,
    graph: ProjectGraph,
    dispatcher: ControlCommandDispatcher,
    controller: FakeWorkflowController,
    review_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
) -> None:
    """A paused project continues with a new run, not a signal to a closed one."""
    _, factory, _ = review_client
    accepted = client.post(
        api(graph.project_id, "/workflow:continue"),
        json={"entry_stage": "shot_generation", "reason": "review_resolved"},
        headers=headers(key="continue-1"),
    )
    assert accepted.status_code == 202
    command_id = accepted.json()["command"]["command_id"]

    dispatcher.run_once()
    command = client.get(
        api(graph.project_id, f"/commands/{command_id}"), headers=OWNER
    ).json()["command"]
    assert command["status"] == "running"
    assert command["workflow_id"] == f"vidgen-project-{graph.project_id}"
    started = controller.started[command["workflow_id"]]
    assert started.entry_stage == "shot_generation"
    assert started.generation_run_id is not None

    with factory() as session:
        runs = GenerationRunService(session).history(graph.project_id)
    assert [run.entry_stage for run in runs] == ["shot_generation"]
    assert runs[0].workflow_id == command["workflow_id"]


def test_a_second_continuation_supersedes_the_previous_lineage(
    client: TestClient,
    graph: ProjectGraph,
    dispatcher: ControlCommandDispatcher,
    review_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
) -> None:
    """Previous runs are preserved as history rather than overwritten."""
    _, factory, _ = review_client
    for index, stage in enumerate(("shot_generation", "render")):
        client.post(
            api(graph.project_id, "/workflow:continue"),
            json={"entry_stage": stage, "reason": "operator_request"},
            headers=headers(key=f"continue-{index}"),
        )
        dispatcher.run_once()
    with factory() as session:
        runs = GenerationRunService(session).history(graph.project_id)
        active = GenerationRunService(session).active(graph.project_id)
    assert [run.entry_stage for run in runs] == ["shot_generation", "render"]
    assert runs[0].status == "superseded" and runs[0].active is False
    assert active is not None and active.entry_stage == "render"


def test_a_rerender_that_cannot_resolve_inputs_fails_with_an_actionable_code(
    graph: ProjectGraph,
    dispatcher: ControlCommandDispatcher,
    review_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
) -> None:
    """A rerender goes through the canonical T17b queue, refusal included.

    The review fixture's narration deliberately has no measured audio, so
    ``queue_render_job`` refuses its lineage. The command must carry that
    refusal as a terminal, readable failure rather than sitting queued.
    """
    _, factory, _ = review_client
    with factory() as session:
        project = session.get(Project, graph.project_id)
        assert project is not None
        outcome = ControlPlaneService(session, "owner-a").submit(
            project,
            command_type=ControlCommandType.RENDER_RERENDER,
            target_type=ControlCommandTargetType.PROJECT,
            target_id=project.id,
            idempotency_key="rerender-1",
            payload={},
        )
        session.commit()
        command_id = outcome.command.command_id
    dispatcher.run_once()
    with factory() as session:
        record = session.get(ControlCommandRecord, command_id)
        assert record is not None
        assert record.status == ControlCommandStatus.FAILED.value
        assert record.error_code
        assert record.workflow_id is None


def test_a_running_render_command_settles_from_the_workflow_result(
    graph: ProjectGraph,
    dispatcher: ControlCommandDispatcher,
    controller: FakeWorkflowController,
    review_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
) -> None:
    """Completion comes from the workflow's own state, never from the request."""
    _, factory, _ = review_client
    workflow_id = "vidgen-render-test"
    with factory() as session:
        project = session.get(Project, graph.project_id)
        assert project is not None
        repository = ControlCommandRepository(session)
        record = repository.create(
            ControlCommandRequest(
                project_id=project.id,
                owner_subject="owner-a",
                command_type=ControlCommandType.RENDER_RERENDER,
                target_type=ControlCommandTargetType.PROJECT,
                target_id=project.id,
                idempotency_key="rerender-settle",
                request_hash=request_digest({}),
                upstream_input_identity=IDENTITY,
            )
        ).record
        repository.claim(record, claim_owner="test-dispatcher")
        repository.mark_dispatching(record)
        repository.mark_running(record, workflow_id=workflow_id, run_id=f"{workflow_id}-run")
        session.commit()
        command_id = record.id
    controller.render_states[workflow_id] = RenderActivityResult(
        project_id=graph.project_id,
        render_job_id=graph.render_job_id or uuid4(),
        status="render_complete",
        progress_percent=100,
    )
    assert dispatcher.settle_running() == 1
    with factory() as session:
        settled = session.get(ControlCommandRecord, command_id)
        assert settled is not None
        assert settled.status == ControlCommandStatus.COMPLETED.value
        assert settled.result_summary["render_status"] == "render_complete"
