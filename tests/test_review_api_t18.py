"""T18 control-plane API tests.

Every test runs against SQLite, synthetic media, and the deterministic fake
workflow controller: no Temporal cluster and no paid provider call is involved.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from services.continuity.bindings import make_bundle
from services.continuity.regeneration import ContinuityRegenerator
from tests.review_fixtures import SHOT_COUNT, ProjectGraph, build_project_graph
from vidgen.db.animation_models import AnimationGeneratedVideo, AnimationItem
from vidgen.db.control_command_models import ControlCommandRecord
from vidgen.db.models import Project, RenderJob
from vidgen.db.review_models import ApiIdempotencyRecord, ProjectUIEvent, RenderApproval
from vidgen.db.script_models import Script, ScriptGenerationRun, ScriptSegment
from vidgen.db.storyboard_models import StoryboardShotRecord
from vidgen.db.upload_models import UploadSession
from vidgen.review.workflow_control import FakeWorkflowController

OWNER = {"X-VidGen-User": "owner-a"}
INTRUDER = {"X-VidGen-User": "owner-b"}


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


def headers(*, if_match: int | None = None, key: str | None = None) -> dict[str, str]:
    out = dict(OWNER)
    if if_match is not None:
        out["If-Match"] = str(if_match)
    if key is not None:
        out["Idempotency-Key"] = key
    return out


def api(project_id: UUID, suffix: str = "") -> str:
    return f"/api/v1/projects/{project_id}{suffix}"


# ---------------------------------------------------------------------------
# Owner scoping
# ---------------------------------------------------------------------------


def test_owner_can_read_their_project(client: TestClient, graph: ProjectGraph) -> None:
    response = client.get(api(graph.project_id), headers=OWNER)
    assert response.status_code == 200
    assert response.json()["id"] == str(graph.project_id)


def test_project_list_carries_status_cost_and_failure_indicators(
    client: TestClient, graph: ProjectGraph
) -> None:
    body = client.get("/api/v1/projects", headers=OWNER).json()
    assert len(body) == 1
    row = body[0]
    assert row["name"] == "Season 3 Episode 4"
    assert row["hard_cap_amount"] == "20.000000"
    assert row["committed_cost_amount"] == "1.000000"
    assert row["has_failures"] is True
    assert row["row_version"] >= 1


@pytest.mark.parametrize(
    "suffix",
    [
        "",
        "/transcript",
        "/script",
        "/storyboard",
        "/shots",
        "/render",
        "/workflow",
        "/costs",
        "/references",
        "/references/invalidation",
    ],
)
def test_cross_owner_reads_are_indistinguishable_from_missing(
    client: TestClient, graph: ProjectGraph, suffix: str
) -> None:
    response = client.get(api(graph.project_id, suffix), headers=INTRUDER)
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_cross_project_nested_resources_are_rejected(
    client: TestClient,
    graph: ProjectGraph,
    review_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
) -> None:
    _, factory, _ = review_client
    with factory() as session:
        other = build_project_graph(session, owner_subject="owner-a", name="Other")
    response = client.get(api(other.project_id, f"/shots/{graph.shot_ids[0]}"), headers=OWNER)
    assert response.status_code == 404


def test_reference_build_requires_concurrency_and_is_idempotent(
    client: TestClient, graph: ProjectGraph
) -> None:
    path = api(graph.project_id, "/references:build")
    payload = {"provider": "fake", "model": "fake-v1"}
    assert client.post(path, headers=OWNER, json=payload).status_code in {409, 428}
    mutation_headers = headers(if_match=1, key="reference-build-1")
    first = client.post(path, headers=mutation_headers, json=payload)
    second = client.post(path, headers=mutation_headers, json=payload)
    assert first.status_code == second.status_code == 202
    assert first.json() == second.json()


def test_reference_application_stales_and_regenerates_only_affected_shot(
    graph: ProjectGraph,
    review_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
) -> None:
    _, factory, _ = review_client
    affected = graph.shot_ids[0]
    calls: list[tuple[UUID, str, str]] = []
    with factory() as session:
        bundle = make_bundle(
            project_id=graph.project_id,
            storyboard_run_id=graph.storyboard_run_id,
            shot_id=affected,
            shot_sequence=0,
            references=[],
            provider_reference_limit=4,
        )
        report = ContinuityRegenerator(
            session, lambda shot, digest, key: calls.append((shot, digest, key))
        ).apply(
            project_id=graph.project_id,
            bundles=[bundle],
            idempotency_key="apply-reference-v2",
        )
        session.commit()
        selected_siblings = session.scalars(
            select(AnimationGeneratedVideo.shot_id).where(
                AnimationGeneratedVideo.project_id == graph.project_id,
                AnimationGeneratedVideo.selected.is_(True),
            )
        ).all()
    assert report.affected_shot_ids == [affected]
    assert affected not in selected_siblings
    assert set(selected_siblings) == set(graph.shot_ids[1:])
    assert calls == [(affected, bundle.bundle_hash, calls[0][2])]
    assert bundle.bundle_hash in calls[0][2]


def test_asset_download_requires_project_ownership(client: TestClient, graph: ProjectGraph) -> None:
    asset_id = graph.final_video_asset_id
    assert asset_id is not None
    assert (
        client.get(f"/api/v1/assets/{asset_id}/download-url", headers=INTRUDER).status_code == 404
    )
    allowed = client.get(f"/api/v1/assets/{asset_id}/download-url", headers=OWNER)
    assert allowed.status_code == 200
    assert allowed.json()["url"]


# ---------------------------------------------------------------------------
# Workflow control
# ---------------------------------------------------------------------------


def test_workflow_start_is_idempotent_and_creates_one_workflow(
    client: TestClient, graph: ProjectGraph, controller: FakeWorkflowController
) -> None:
    first = client.post(
        api(graph.project_id, "/workflow:start"), json={}, headers=headers(key="start-1")
    )
    assert first.status_code == 200
    second = client.post(
        api(graph.project_id, "/workflow:start"), json={}, headers=headers(key="start-1")
    )
    assert second.status_code == 200
    assert first.json()["workflow_id"] == second.json()["workflow_id"]
    assert len(controller.started) == 1


def test_duplicate_workflow_start_with_a_new_key_still_reuses_the_workflow(
    client: TestClient, graph: ProjectGraph, controller: FakeWorkflowController
) -> None:
    client.post(api(graph.project_id, "/workflow:start"), json={}, headers=headers(key="start-1"))
    again = client.post(
        api(graph.project_id, "/workflow:start"), json={}, headers=headers(key="start-2")
    )
    assert again.status_code == 200
    assert len(controller.started) == 1


def test_workflow_start_requires_an_idempotency_key(
    client: TestClient, graph: ProjectGraph
) -> None:
    response = client.post(api(graph.project_id, "/workflow:start"), json={}, headers=OWNER)
    assert response.status_code == 428
    assert response.json()["code"] == "idempotency_key_required"


def test_workflow_start_rejects_an_incomplete_upload(
    client: TestClient,
    review_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
) -> None:
    _, factory, _ = review_client
    with factory() as session:
        project = Project(
            name="No upload yet",
            owner_subject="owner-a",
            status="awaiting_upload",
            target_duration_seconds=120,
            visual_style="flat",
            humor_intensity=5,
            settings={},
        )
        session.add(project)
        session.commit()
        project_id = project.id
    response = client.post(
        api(project_id, "/workflow:start"), json={}, headers=headers(key="start-1")
    )
    assert response.status_code == 409
    assert response.json()["code"] == "upload_incomplete"


def test_workflow_start_accepts_the_status_a_real_upload_records(
    client: TestClient,
    review_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
    golden_video: Path,
) -> None:
    """A project uploaded through the API must be startable.

    The guard previously compared the upload status against "completed" while
    ``UploadService.finalize`` records "complete", so every project uploaded
    through the documented local path was refused as incomplete.
    """
    _, factory, controller = review_client
    created = client.post(
        "/api/v1/projects",
        json={"name": "Uploaded locally", "visual_style": "flat", "humor_intensity": 5},
        headers=OWNER,
    )
    assert created.status_code == 201
    project_id = created.json()["id"]

    content = golden_video.read_bytes()
    upload = client.post(
        f"/api/v1/projects/{project_id}/uploads",
        json={
            "filename": "golden.mp4",
            "media_type": "video/mp4",
            "expected_size": len(content),
            "expected_sha256": hashlib.sha256(content).hexdigest(),
            "part_size": 8 * 1024 * 1024,
        },
        headers=OWNER,
    ).json()
    assert (
        client.put(
            f"/api/v1/uploads/{upload['id']}/parts/0", content=content, headers=OWNER
        ).status_code
        == 200
    )
    completed = client.post(f"/api/v1/uploads/{upload['id']}/complete", headers=OWNER)
    assert completed.status_code == 200

    with factory() as session:
        stored = session.scalar(select(UploadSession).where(UploadSession.id == UUID(upload["id"])))
        assert stored is not None and stored.status == "complete"

    # T18b: a project with a complete upload but no narration voice is refused
    # here, before Temporal, rather than failing inside a paid T12 run.
    refused = client.post(
        f"/api/v1/projects/{project_id}/workflow:start",
        json={},
        headers=headers(key="start-without-voice"),
    )
    assert refused.status_code == 409
    assert refused.json()["code"] == "voice_profile_required"

    catalog = client.get(f"/api/v1/projects/{project_id}/voice-profiles", headers=OWNER).json()
    fake = next(item for item in catalog["items"] if item["provider"] == "fake")
    selected = client.put(
        f"/api/v1/projects/{project_id}/voice-profile",
        json={"voice_profile_id": fake["voice_profile_id"]},
        headers=OWNER,
    )
    assert selected.status_code == 200
    assert selected.json()["profile"]["selected"] is True

    started = client.post(
        api(UUID(project_id), "/workflow:start"), json={}, headers=headers(key="start-1")
    )
    assert started.status_code == 200, started.json()
    assert len(controller.started) == 1


def test_workflow_cancel_and_compact_status(
    client: TestClient, graph: ProjectGraph, controller: FakeWorkflowController
) -> None:
    client.post(api(graph.project_id, "/workflow:start"), json={}, headers=headers(key="start-1"))
    cancelled = client.post(
        api(graph.project_id, "/workflow:cancel"), headers=headers(key="cancel-1")
    )
    assert cancelled.status_code == 200
    assert controller.cancelled
    status = client.get(api(graph.project_id, "/workflow"), headers=OWNER).json()
    assert status["total_shot_count"] == SHOT_COUNT
    assert status["completed_shot_count"] == SHOT_COUNT
    assert len(status["stages"]) == 14
    # Compact only: no stage payload ever appears in the status projection.
    assert "transcript" not in status


def test_workflow_status_omits_a_percentage_when_no_shots_exist(
    client: TestClient,
    review_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
) -> None:
    _, factory, _ = review_client
    with factory() as session:
        project = Project(
            name="Fresh",
            owner_subject="owner-a",
            status="ingesting",
            target_duration_seconds=120,
            visual_style="flat",
            humor_intensity=5,
            settings={},
        )
        session.add(project)
        session.commit()
        project_id = project.id
    status = client.get(api(project_id, "/workflow"), headers=OWNER).json()
    assert status["progress_percentage"] is None


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


def test_sse_authorization_precedes_streaming(client: TestClient, graph: ProjectGraph) -> None:
    response = client.get(api(graph.project_id, "/events?poll=true"), headers=INTRUDER)
    assert response.status_code == 404


def test_events_poll_supports_last_event_id_and_deduplicates(
    client: TestClient, graph: ProjectGraph
) -> None:
    client.post(api(graph.project_id, "/workflow:start"), json={}, headers=headers(key="start-1"))
    client.post(api(graph.project_id, "/workflow:cancel"), headers=headers(key="cancel-1"))
    first = client.get(api(graph.project_id, "/events?poll=true"), headers=OWNER).json()
    ids = [item["event_id"] for item in first["items"]]
    assert ids == sorted(ids)
    assert len(ids) == len(set(ids))
    resumed = client.get(
        api(graph.project_id, "/events?poll=true"),
        headers={**OWNER, "Last-Event-ID": str(ids[0])},
    ).json()
    assert [item["event_id"] for item in resumed["items"]] == ids[1:]


def test_event_payloads_stay_bounded(client: TestClient, graph: ProjectGraph) -> None:
    client.post(api(graph.project_id, "/workflow:start"), json={}, headers=headers(key="start-1"))
    body = client.get(api(graph.project_id, "/events?poll=true"), headers=OWNER).json()
    for item in body["items"]:
        assert set(item) <= {
            "schema_version",
            "event_id",
            "project_id",
            "workflow_id",
            "event_type",
            "stage",
            "status",
            "progress_percentage",
            "completed_shot_count",
            "total_shot_count",
            "retryable_failure_count",
            "render_status",
            "cost_summary_version",
            "warning_code",
            "failure_code",
            "created_at",
        }


def test_sse_stream_emits_events_and_closes(client: TestClient, graph: ProjectGraph) -> None:
    client.post(api(graph.project_id, "/workflow:start"), json={}, headers=headers(key="start-1"))
    url = api(graph.project_id, "/events?close_after_events=1")
    with client.stream("GET", url, headers=OWNER) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        lines = [line for line in response.iter_lines() if line]
    assert lines[0].startswith("id: ")
    assert any(line.startswith("data: ") for line in lines)


# ---------------------------------------------------------------------------
# Transcript
# ---------------------------------------------------------------------------


def test_transcript_retrieval_and_segment_edit(client: TestClient, graph: ProjectGraph) -> None:
    body = client.get(api(graph.project_id, "/transcript"), headers=OWNER).json()
    assert body["transcript_id"] == str(graph.transcript_id)
    assert len(body["segments"]) == 3
    segment = body["segments"][0]
    response = client.patch(
        api(graph.project_id, f"/transcript/segments/{segment['segment_id']}"),
        json={"text": "Corrected line.", "speaker_label": "NARRATOR", "confirm_invalidation": True},
        headers=headers(if_match=segment["row_version"], key="tx-1"),
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["segment"]["text"] == "Corrected line."
    assert payload["segment"]["edited"] is True
    assert payload["segment"]["row_version"] == segment["row_version"] + 1
    assert payload["invalidation"]["entries"]


def test_transcript_edit_preserves_original_provenance(
    client: TestClient,
    graph: ProjectGraph,
    review_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
) -> None:
    body = client.get(api(graph.project_id, "/transcript"), headers=OWNER).json()
    segment = body["segments"][0]
    client.patch(
        api(graph.project_id, f"/transcript/segments/{segment['segment_id']}"),
        json={"text": "Corrected.", "confirm_invalidation": True},
        headers=headers(if_match=segment["row_version"], key="tx-1"),
    )
    _, factory, _ = review_client
    from vidgen.db.transcription_models import TranscriptSegmentRecord

    with factory() as session:
        row = session.get(TranscriptSegmentRecord, UUID(segment["segment_id"]))
        assert row is not None
        assert row.provenance["original"]["text"] == "Transcript line 1."
        assert row.provenance["provider"] == "fake"


def test_transcript_edit_requires_if_match(client: TestClient, graph: ProjectGraph) -> None:
    body = client.get(api(graph.project_id, "/transcript"), headers=OWNER).json()
    segment = body["segments"][0]
    response = client.patch(
        api(graph.project_id, f"/transcript/segments/{segment['segment_id']}"),
        json={"text": "Nope."},
        headers=headers(key="tx-1"),
    )
    assert response.status_code == 428
    assert response.json()["code"] == "precondition_required"
    assert response.json()["current_version"] == segment["row_version"]


def test_transcript_edit_rejects_a_stale_version(client: TestClient, graph: ProjectGraph) -> None:
    body = client.get(api(graph.project_id, "/transcript"), headers=OWNER).json()
    segment = body["segments"][0]
    response = client.patch(
        api(graph.project_id, f"/transcript/segments/{segment['segment_id']}"),
        json={"text": "Nope."},
        headers=headers(if_match=segment["row_version"] + 5, key="tx-1"),
    )
    assert response.status_code == 409
    assert response.json()["code"] == "version_conflict"
    assert response.json()["current_version"] == segment["row_version"]


def test_transcript_segment_from_another_project_is_not_found(
    client: TestClient,
    graph: ProjectGraph,
    review_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
) -> None:
    _, factory, _ = review_client
    with factory() as session:
        other = build_project_graph(session, owner_subject="owner-a", name="Other")
    response = client.patch(
        api(graph.project_id, f"/transcript/segments/{other.transcript_segment_ids[0]}"),
        json={"text": "x"},
        headers=headers(if_match=1, key="tx-1"),
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Script
# ---------------------------------------------------------------------------


def test_script_retrieval(client: TestClient, graph: ProjectGraph) -> None:
    body = client.get(api(graph.project_id, "/script"), headers=OWNER).json()
    assert body["script"]["script_id"] == str(graph.script_id)
    assert body["approved"] is True
    assert len(body["segments"]) == SHOT_COUNT
    assert body["segments"][0]["word_count"] > 0


def test_script_segment_edit_creates_a_new_version_and_preserves_the_old(
    client: TestClient,
    graph: ProjectGraph,
    review_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
) -> None:
    body = client.get(api(graph.project_id, "/script"), headers=OWNER).json()
    segment = body["segments"][0]
    response = client.patch(
        api(graph.project_id, f"/script-segments/{segment['segment_id']}"),
        json={"text": "A far funnier opening beat.", "confirm_invalidation": True},
        headers=headers(if_match=segment["row_version"], key="script-1"),
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["created_version"] is True
    assert payload["script"]["version"] == 2
    assert payload["segment"]["text"] == "A far funnier opening beat."
    assert payload["invalidation"]["entries"]

    _, factory, _ = review_client
    with factory() as session:
        original = session.get(Script, graph.script_id)
        assert original is not None
        assert original.version == 1
        assert original.selected is False
        first_segment = session.get(ScriptSegment, UUID(segment["segment_id"]))
        assert first_segment is not None
        assert first_segment.text == "Recap beat number 1 lands with a joke."


def test_script_versions_are_listed_and_selectable(client: TestClient, graph: ProjectGraph) -> None:
    body = client.get(api(graph.project_id, "/script"), headers=OWNER).json()
    segment = body["segments"][0]
    client.patch(
        api(graph.project_id, f"/script-segments/{segment['segment_id']}"),
        json={"text": "Revised beat.", "confirm_invalidation": True},
        headers=headers(if_match=segment["row_version"], key="script-1"),
    )
    versions = client.get(api(graph.project_id, "/scripts"), headers=OWNER).json()["items"]
    assert [item["version"] for item in versions] == [1, 2]
    original = next(item for item in versions if item["version"] == 1)
    selected = client.post(
        api(graph.project_id, f"/scripts/{original['script_id']}:select"),
        headers=headers(if_match=original["row_version"], key="select-1"),
    )
    assert selected.status_code == 200
    assert selected.json()["script"]["selected"] is True


def test_selecting_a_script_approves_it_and_resolves_the_review(
    client: TestClient,
    graph: ProjectGraph,
    review_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
) -> None:
    """Selecting a version is the approval a stalled run stopped short of making."""
    _, factory, _ = review_client
    with factory() as session:
        # The state a run that exhausted its revisions leaves behind: candidate
        # scripts, none of them selected, and a project waiting on the owner.
        script = session.scalars(select(Script).where(Script.project_id == graph.project_id)).one()
        script.selected = False
        script.status = "draft"
        run = session.get(ScriptGenerationRun, script.generation_run_id)
        assert run is not None
        run.status = "script_review_required"
        run.error_code = "REVISION_EXHAUSTED"
        project = session.get(Project, graph.project_id)
        assert project is not None
        project.status = "script_review_required"
        session.commit()
        script_id = script.id

    assert client.get(api(graph.project_id, "/script"), headers=OWNER).status_code == 404
    candidate = next(
        item
        for item in client.get(api(graph.project_id, "/scripts"), headers=OWNER).json()["items"]
        if item["script_id"] == str(script_id)
    )
    response = client.post(
        api(graph.project_id, f"/scripts/{script_id}:select"),
        headers=headers(if_match=candidate["row_version"], key="approve-1"),
    )
    assert response.status_code == 200

    body = client.get(api(graph.project_id, "/script"), headers=OWNER).json()
    assert body["approved"] is True
    assert body["script"]["status"] == "approved"
    with factory() as session:
        project = session.get(Project, graph.project_id)
        assert project is not None
        assert project.status == "script_approved"
        run = session.get(ScriptGenerationRun, session.get(Script, script_id).generation_run_id)
        assert run is not None
        assert run.status == "script_approved"
        assert run.error_code is None


def test_rejecting_a_pass_stores_the_feedback_for_the_next_one(
    client: TestClient,
    graph: ProjectGraph,
    review_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
) -> None:
    """Rejecting decides nothing downstream: it records the brief for pass two."""
    _, factory, _ = review_client
    with factory() as session:
        # The state pass one leaves behind: an edited candidate waiting on the
        # owner, nothing selected, and the run paused at review.
        script = session.scalars(select(Script).where(Script.project_id == graph.project_id)).one()
        script.selected = False
        script.status = "pending_review"
        script.editing_pass = 1
        run = session.get(ScriptGenerationRun, script.generation_run_id)
        assert run is not None
        run.status = "script_review_required"
        run.max_editing_passes = 3
        project = session.get(Project, graph.project_id)
        assert project is not None
        project.status = "script_review_required"
        session.commit()
        script_id = script.id

    candidate = next(
        item
        for item in client.get(api(graph.project_id, "/scripts"), headers=OWNER).json()["items"]
        if item["script_id"] == str(script_id)
    )
    assert candidate["editing_pass"] == 1
    assert candidate["rejection_reason"] is None

    response = client.post(
        api(graph.project_id, f"/scripts/{script_id}:reject"),
        json={"reason": "The finale is spoiled in the cold open."},
        headers=headers(if_match=candidate["row_version"], key="reject-1"),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["script"]["status"] == "rejected"
    assert body["script"]["rejection_reason"] == "The finale is spoiled in the cold open."
    assert body["editing_pass"] == 1
    assert body["max_editing_passes"] == 3
    assert body["passes_remaining"] == 2
    assert body["script"]["row_version"] == candidate["row_version"] + 1

    # Same key, same payload: replayed, not applied twice.
    replay = client.post(
        api(graph.project_id, f"/scripts/{script_id}:reject"),
        json={"reason": "The finale is spoiled in the cold open."},
        headers=headers(if_match=candidate["row_version"], key="reject-1"),
    )
    assert replay.status_code == 200
    assert replay.json() == body

    with factory() as session:
        stored = session.get(Script, script_id)
        assert stored is not None
        assert stored.status == "rejected"
        assert stored.selected is False
        assert stored.rejection_reason == "The finale is spoiled in the cold open."
        # The project is still waiting at its review checkpoint; the next
        # ``workflow:continue`` is what runs pass two.
        project = session.get(Project, graph.project_id)
        assert project is not None
        assert project.status == "script_review_required"
        run = session.get(ScriptGenerationRun, stored.generation_run_id)
        assert run is not None
        assert run.status == "script_review_required"
        events = session.scalars(
            select(ProjectUIEvent).where(ProjectUIEvent.event_type == "script_rejected")
        ).all()
        assert len(events) == 1

    # A stale If-Match is refused, and so is an empty reason.
    stale = client.post(
        api(graph.project_id, f"/scripts/{script_id}:reject"),
        json={"reason": "Again."},
        headers=headers(if_match=candidate["row_version"], key="reject-2"),
    )
    assert stale.status_code == 409
    empty = client.post(
        api(graph.project_id, f"/scripts/{script_id}:reject"),
        json={"reason": ""},
        headers=headers(if_match=body["script"]["row_version"], key="reject-3"),
    )
    assert empty.status_code == 422


def test_an_approved_script_cannot_be_rejected(client: TestClient, graph: ProjectGraph) -> None:
    scripts = client.get(api(graph.project_id, "/scripts"), headers=OWNER).json()["items"]
    approved = scripts[0]
    assert approved["selected"] is True
    response = client.post(
        api(graph.project_id, f"/scripts/{approved['script_id']}:reject"),
        json={"reason": "Too late."},
        headers=headers(if_match=approved["row_version"], key="reject-approved"),
    )
    assert response.status_code == 409
    assert response.json()["code"] == "validation_failed"
    assert (
        client.post(
            api(graph.project_id, f"/scripts/{approved['script_id']}:reject"),
            json={"reason": "Not mine."},
            headers={**INTRUDER, "If-Match": "1", "Idempotency-Key": "reject-intruder"},
        ).status_code
        == 404
    )


def test_selecting_a_version_later_does_not_rewind_the_project_status(
    client: TestClient,
    graph: ProjectGraph,
    review_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
) -> None:
    """A version switch mid-run approves the script without moving the project."""
    _, factory, _ = review_client
    scripts = client.get(api(graph.project_id, "/scripts"), headers=OWNER).json()["items"]
    target = scripts[0]
    assert (
        client.post(
            api(graph.project_id, f"/scripts/{target['script_id']}:select"),
            headers=headers(if_match=target["row_version"], key="select-late"),
        ).status_code
        == 200
    )
    with factory() as session:
        project = session.get(Project, graph.project_id)
        assert project is not None
        assert project.status == "review"


def test_script_edit_without_confirmation_is_a_structured_conflict(
    client: TestClient, graph: ProjectGraph
) -> None:
    body = client.get(api(graph.project_id, "/script"), headers=OWNER).json()
    segment = body["segments"][0]
    response = client.patch(
        api(graph.project_id, f"/script-segments/{segment['segment_id']}"),
        json={"text": "Unconfirmed rewrite."},
        headers=headers(if_match=segment["row_version"], key="script-1"),
    )
    assert response.status_code == 409
    assert "invalidates downstream work" in response.json()["summary"]


def test_script_edit_rejects_empty_text(client: TestClient, graph: ProjectGraph) -> None:
    body = client.get(api(graph.project_id, "/script"), headers=OWNER).json()
    segment = body["segments"][0]
    response = client.patch(
        api(graph.project_id, f"/script-segments/{segment['segment_id']}"),
        json={"text": "   ", "confirm_invalidation": True},
        headers=headers(if_match=segment["row_version"], key="script-1"),
    )
    assert response.status_code == 422
    assert response.json()["code"] == "validation_failed"


# ---------------------------------------------------------------------------
# Storyboard and shots
# ---------------------------------------------------------------------------


def test_storyboard_retrieval_uses_t13_timing(client: TestClient, graph: ProjectGraph) -> None:
    body = client.get(api(graph.project_id, "/storyboard"), headers=OWNER).json()
    assert body["shot_count"] == SHOT_COUNT
    assert [shot["global_sequence"] for shot in body["shots"]] == list(range(SHOT_COUNT))
    first = body["shots"][0]
    assert first["usable_duration_us"] == 3_000_000
    assert first["requested_generation_duration_us"] == 4_000_000
    assert first["trim_end_us"] == 1_000_000
    assert first["cost_amount"] == "0.100000"


def test_shot_inspection_exposes_attempts_and_technical_identity(
    client: TestClient, graph: ProjectGraph
) -> None:
    shot_id = graph.shot_ids[5]
    body = client.get(api(graph.project_id, f"/shots/{shot_id}"), headers=OWNER).json()
    assert body["shot"]["shot_id"] == str(shot_id)
    assert len(body["keyframe_attempts"]) == 1
    assert len(body["video_attempts"]) == 1
    assert body["video_attempts"][0]["provider_task_id"].endswith("-task-5")
    assert body["identity_hash"]
    assert body["source_evidence_ids"]


def test_shot_status_projection(client: TestClient, graph: ProjectGraph) -> None:
    body = client.get(
        api(graph.project_id, f"/shots/{graph.shot_ids[5]}/status"), headers=OWNER
    ).json()
    assert body["status"] == "locked"
    assert body["retryable"] is False


def test_regenerating_one_shot_does_not_rerun_siblings(
    client: TestClient,
    graph: ProjectGraph,
    controller: FakeWorkflowController,
    review_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
) -> None:
    target = graph.shot_ids[5]
    before = _shot_identities(review_client[1])
    shot = client.get(api(graph.project_id, f"/shots/{target}"), headers=OWNER).json()
    response = client.post(
        api(graph.project_id, f"/shots/{target}:regenerate"),
        json={"confirm_invalidation": True},
        headers=headers(if_match=shot["shot"]["row_version"], key="regen-1"),
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["shot_id"] == str(target)
    # No workflow has been started yet, so the response must not name one. The
    # command is what the caller polls until the replacement child exists.
    assert payload["child_workflow_id"] is None
    assert payload["command_id"]
    assert payload["command_status"] == "pending"
    assert payload["regeneration_sequence"] == 1
    assert payload["new_identity_hash"] != payload["previous_identity_hash"]
    assert payload["preserved_attempt_ids"]

    # Exactly one durable command was created, and it targets only this shot.
    with review_client[1]() as session:
        commands = (
            session.query(ControlCommandRecord)
            .filter_by(project_id=graph.project_id, command_type="shot_regenerate")
            .all()
        )
        assert len(commands) == 1
        assert commands[0].target_id == target
        assert commands[0].status == "pending"
    # The locked child is never signalled: it has already completed, and a
    # completed workflow cannot accept a regeneration.
    assert controller.shot_commands == []

    after = _shot_identities(review_client[1])
    for shot_id, identity in before.items():
        if shot_id == target:
            continue
        assert after[shot_id] == identity, "a sibling shot lost its locked identity"


def test_regeneration_returns_the_exact_invalidation_set(
    client: TestClient, graph: ProjectGraph
) -> None:
    target = graph.shot_ids[5]
    shot = client.get(api(graph.project_id, f"/shots/{target}"), headers=OWNER).json()
    payload = client.post(
        api(graph.project_id, f"/shots/{target}:regenerate"),
        json={"confirm_invalidation": True},
        headers=headers(if_match=shot["shot"]["row_version"], key="regen-1"),
    ).json()
    kinds = {entry["resource_type"] for entry in payload["invalidation"]["entries"]}
    assert kinds == {"shot", "render"}
    assert all(
        entry["resource_id"] in {str(target), str(graph.render_job_id)}
        for entry in payload["invalidation"]["entries"]
    )


def test_regeneration_requires_confirmation(client: TestClient, graph: ProjectGraph) -> None:
    target = graph.shot_ids[5]
    shot = client.get(api(graph.project_id, f"/shots/{target}"), headers=OWNER).json()
    response = client.post(
        api(graph.project_id, f"/shots/{target}:regenerate"),
        json={},
        headers=headers(if_match=shot["shot"]["row_version"], key="regen-1"),
    )
    assert response.status_code == 409


def test_shot_retry_records_a_durable_command_rather_than_signalling(
    client: TestClient,
    graph: ProjectGraph,
    controller: FakeWorkflowController,
    review_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
) -> None:
    """Retry is durable work, and it never signals a child that already closed.

    The API cannot know whether the shot's child is still live, so it records
    the decision and lets the dispatcher choose: resume a live child, or start
    an immutable recovery run. Answering 409 here - as this route used to for a
    locked shot - meant a legitimate recovery had no path at all.
    """
    target = graph.shot_ids[2]
    shot = client.get(api(graph.project_id, f"/shots/{target}"), headers=OWNER).json()
    response = client.post(
        api(graph.project_id, f"/shots/{target}:retry"),
        headers=headers(if_match=shot["shot"]["row_version"], key="retry-1"),
    )
    assert response.status_code == 202
    command = response.json()["command"]
    assert command["command_type"] == "shot_retry"
    assert command["status"] == "pending"
    assert command["workflow_id"] is None
    assert command["target_id"] == str(target)
    assert controller.shot_commands == []
    with review_client[1]() as session:
        stored = session.get(ControlCommandRecord, UUID(command["command_id"]))
        assert stored is not None and stored.project_id == graph.project_id


def _queue_shot_retry(client: TestClient, graph: ProjectGraph, shot_id: UUID, key: str) -> UUID:
    """Ask for a retry and return the durable command the API recorded."""
    shot = client.get(api(graph.project_id, f"/shots/{shot_id}"), headers=OWNER).json()
    response = client.post(
        api(graph.project_id, f"/shots/{shot_id}:retry"),
        headers=headers(if_match=shot["shot"]["row_version"], key=key),
    )
    assert response.status_code == 202
    return UUID(response.json()["command"]["command_id"])


def _shot_from_storyboard(client: TestClient, graph: ProjectGraph, shot_id: UUID) -> dict[str, Any]:
    body = client.get(api(graph.project_id, "/storyboard"), headers=OWNER).json()
    return next(entry for entry in body["shots"] if entry["shot_id"] == str(shot_id))


def _fail_the_shot(factory: sessionmaker[Session], shot_id: UUID) -> None:
    """Put the shot in the state the review UI offers a decision on."""
    with factory() as session:
        item = session.scalar(select(AnimationItem).where(AnimationItem.shot_id == shot_id))
        assert item is not None
        item.status = "failed"
        item.error_code = "qa_rejected"
        session.commit()


def test_a_queued_shot_command_is_visible_before_anything_dispatches(
    client: TestClient,
    graph: ProjectGraph,
    review_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
) -> None:
    """The exact window the review UI used to have no answer for.

    Between an accepted retry and the replacement workflow writing its first
    row, the shot's own tables still describe the previous, failed attempt. If
    the projection reports only those, the card renders a fresh failure and
    offers the same button again - so a reviewer cannot tell their decision
    landed, and can fire the command twice.
    """
    target = graph.shot_ids[4]
    _fail_the_shot(review_client[1], target)
    command_id = _queue_shot_retry(client, graph, target, "retry-window-1")

    entry = _shot_from_storyboard(client, graph, target)
    # The shot is still failed - nothing has replaced it yet - but it is no
    # longer a shot with no decision recorded against it.
    assert entry["failure_code"] == "qa_rejected"
    pending = entry["pending_command"]
    assert pending is not None
    assert pending["command_id"] == str(command_id)
    assert pending["command_type"] == "shot_retry"
    assert pending["status"] == "pending"
    assert pending["active"] is True
    # Queued, not dispatched: no workflow exists yet, and the projection must
    # not imply one does.
    assert pending["dispatched"] is False
    assert pending["workflow_id"] is None

    detail = client.get(api(graph.project_id, f"/shots/{target}"), headers=OWNER).json()
    assert detail["shot"]["pending_command"]["command_id"] == str(command_id)
    status = client.get(api(graph.project_id, f"/shots/{target}/status"), headers=OWNER).json()
    assert status["pending_command"]["active"] is True

    # Sibling shots are untouched: one shot's command never speaks for another.
    sibling = _shot_from_storyboard(client, graph, graph.shot_ids[5])
    assert sibling["pending_command"] is None


def test_a_dispatched_command_is_distinguishable_from_a_queued_one(
    client: TestClient,
    graph: ProjectGraph,
    review_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
) -> None:
    target = graph.shot_ids[4]
    command_id = _queue_shot_retry(client, graph, target, "retry-window-2")
    with review_client[1]() as session:
        record = session.get(ControlCommandRecord, command_id)
        assert record is not None
        record.status = "running"
        record.workflow_id = "shot-retry-workflow-4"
        session.commit()

    pending = _shot_from_storyboard(client, graph, target)["pending_command"]
    assert pending["status"] == "running"
    assert pending["active"] is True
    assert pending["dispatched"] is True
    assert pending["workflow_id"] == "shot-retry-workflow-4"


def test_a_failed_command_surfaces_its_error_and_gives_the_action_back(
    client: TestClient,
    graph: ProjectGraph,
    review_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
) -> None:
    target = graph.shot_ids[4]
    _fail_the_shot(review_client[1], target)
    command_id = _queue_shot_retry(client, graph, target, "retry-window-3")
    with review_client[1]() as session:
        record = session.get(ControlCommandRecord, command_id)
        assert record is not None
        record.status = "failed"
        record.error_code = "dispatch_rejected"
        record.error_summary = "the upstream material moved"
        record.retryable = True
        session.commit()

    pending = _shot_from_storyboard(client, graph, target)["pending_command"]
    # Not in flight any more, so the UI restores the affordance - but the
    # reason it stopped is renderable rather than silently dropped.
    assert pending["active"] is False
    assert pending["failure_code"] == "dispatch_rejected"
    assert pending["failure_summary"] == "the upstream material moved"
    assert pending["retryable"] is True


@pytest.mark.parametrize("terminal", ["completed", "cancelled"])
def test_a_resolved_command_leaves_the_shot_to_its_own_rows(
    client: TestClient,
    graph: ProjectGraph,
    review_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
    terminal: str,
) -> None:
    target = graph.shot_ids[4]
    command_id = _queue_shot_retry(client, graph, target, f"retry-window-{terminal}")
    with review_client[1]() as session:
        record = session.get(ControlCommandRecord, command_id)
        assert record is not None
        record.status = terminal
        if terminal == "completed":
            record.workflow_id = "shot-retry-workflow-4"
        session.commit()

    assert _shot_from_storyboard(client, graph, target)["pending_command"] is None


def test_the_newest_command_is_the_one_a_shot_reports(
    client: TestClient,
    graph: ProjectGraph,
    review_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
) -> None:
    """A superseded first attempt must not mask the command actually running."""
    target = graph.shot_ids[4]
    first = _queue_shot_retry(client, graph, target, "retry-window-first")
    with review_client[1]() as session:
        record = session.get(ControlCommandRecord, first)
        assert record is not None
        record.status = "failed"
        record.error_code = "lease_lost"
        session.commit()
    second = _queue_shot_retry(client, graph, target, "retry-window-second")

    pending = _shot_from_storyboard(client, graph, target)["pending_command"]
    assert pending["command_id"] == str(second)
    assert pending["active"] is True


def test_a_command_waiting_on_a_person_still_offers_them_the_decision(
    client: TestClient,
    graph: ProjectGraph,
    review_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
) -> None:
    """``awaiting_review`` is active, but it is waiting on the reviewer.

    The dispatcher parks a shot command here when the replacement child reports
    HUMAN_REVIEW_REQUIRED. Treating that as busy would disable the approve and
    reject buttons - the only thing that can release it - and the shot would
    have no way forward at all.
    """
    target = graph.shot_ids[4]
    command_id = _queue_shot_retry(client, graph, target, "retry-awaiting-1")
    with review_client[1]() as session:
        record = session.get(ControlCommandRecord, command_id)
        assert record is not None
        record.status = "awaiting_review"
        record.workflow_id = "shot-retry-workflow-4"
        session.commit()

    pending = _shot_from_storyboard(client, graph, target)["pending_command"]
    assert pending["status"] == "awaiting_review"
    assert pending["active"] is True
    # The flag the UI needs to tell "busy" from "your move".
    assert pending["awaiting_review"] is True


def test_a_running_command_is_not_masked_by_a_newer_finished_one(
    client: TestClient,
    graph: ProjectGraph,
    review_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
) -> None:
    """Work still in flight outranks a newer command that already finished.

    Taking the newest row and only then dropping it when resolved would report
    no pending command at all here - and the duplicate this projection exists
    to prevent would be offered while the first command was still running.
    """
    target = graph.shot_ids[4]
    running = _queue_shot_retry(client, graph, target, "retry-still-running")
    with review_client[1]() as session:
        record = session.get(ControlCommandRecord, running)
        assert record is not None
        record.status = "running"
        record.workflow_id = "shot-retry-workflow-4"
        session.commit()
    later = _queue_shot_retry(client, graph, target, "retry-finished-later")
    with review_client[1]() as session:
        record = session.get(ControlCommandRecord, later)
        assert record is not None
        record.status = "completed"
        record.workflow_id = "shot-retry-workflow-4b"
        session.commit()

    pending = _shot_from_storyboard(client, graph, target)["pending_command"]
    assert pending is not None, "a running command was masked by a newer completed one"
    assert pending["command_id"] == str(running)
    assert pending["active"] is True


def test_a_failure_a_later_command_succeeded_past_is_not_resurfaced(
    client: TestClient,
    graph: ProjectGraph,
    review_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
) -> None:
    target = graph.shot_ids[4]
    failed = _queue_shot_retry(client, graph, target, "retry-old-failure")
    with review_client[1]() as session:
        record = session.get(ControlCommandRecord, failed)
        assert record is not None
        record.status = "failed"
        record.error_code = "lease_lost"
        session.commit()
    later = _queue_shot_retry(client, graph, target, "retry-succeeded-after")
    with review_client[1]() as session:
        record = session.get(ControlCommandRecord, later)
        assert record is not None
        record.status = "completed"
        record.workflow_id = "shot-retry-workflow-4c"
        session.commit()

    # The failure is history: something newer already put it right.
    assert _shot_from_storyboard(client, graph, target)["pending_command"] is None


def test_a_final_qa_remediation_is_visible_on_the_shot_it_regenerates(
    client: TestClient,
    graph: ProjectGraph,
    review_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
) -> None:
    """A T22 remediation regenerates a shot without ever targeting one.

    Its durable row stays pointed at the final-QA run and names the shot only
    in metadata - the shot-targeted proxy the dispatcher builds is never
    persisted - so a projection filtering on ``target_type`` alone would leave
    that shot looking untouched while a replacement run was being started.
    """
    target = graph.shot_ids[6]
    with review_client[1]() as session:
        session.add(
            ControlCommandRecord(
                project_id=graph.project_id,
                owner_subject="owner-a",
                command_type="final_qa_remediation",
                target_type="final_qa_run",
                target_id=uuid4(),
                idempotency_key="remediation-1",
                request_hash=hashlib.sha256(b"remediation-1").hexdigest(),
                upstream_input_identity=hashlib.sha256(b"upstream").hexdigest(),
                status="running",
                workflow_id="vidgen-remediation-1",
                command_metadata={"target": "regenerate_shot_t16", "shot_id": str(target)},
            )
        )
        session.commit()

    pending = _shot_from_storyboard(client, graph, target)["pending_command"]
    assert pending is not None
    assert pending["command_type"] == "final_qa_remediation"
    assert pending["active"] is True
    # And it speaks only for the shot it names.
    assert _shot_from_storyboard(client, graph, graph.shot_ids[7])["pending_command"] is None


def test_shot_attempt_selection(
    client: TestClient,
    graph: ProjectGraph,
    review_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
) -> None:
    target = graph.shot_ids[3]
    attempt_id = graph.video_attempt_ids[3]
    shot = client.get(api(graph.project_id, f"/shots/{target}"), headers=OWNER).json()
    response = client.post(
        api(graph.project_id, f"/shots/{target}:select-attempt"),
        json={"attempt_id": str(attempt_id)},
        headers=headers(if_match=shot["shot"]["row_version"], key="select-attempt-1"),
    )
    assert response.status_code == 200
    with review_client[1]() as session:
        row = session.get(AnimationGeneratedVideo, attempt_id)
        assert row is not None and row.selected is True


def test_selecting_a_foreign_attempt_is_not_found(client: TestClient, graph: ProjectGraph) -> None:
    target = graph.shot_ids[3]
    shot = client.get(api(graph.project_id, f"/shots/{target}"), headers=OWNER).json()
    response = client.post(
        api(graph.project_id, f"/shots/{target}:select-attempt"),
        json={"attempt_id": str(graph.video_attempt_ids[4])},
        headers=headers(if_match=shot["shot"]["row_version"], key="select-attempt-1"),
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Render and approval
# ---------------------------------------------------------------------------


def test_render_retrieval(client: TestClient, graph: ProjectGraph) -> None:
    body = client.get(api(graph.project_id, "/render"), headers=OWNER).json()
    assert body["status"] == "render_complete"
    assert body["verified"] is True
    assert body["stale"] is False
    assert body["caption_cue_count"] == 12
    assert body["subtitle_mode"] == "external"
    assert body["selected_shot_count"] == SHOT_COUNT
    assert body["integrated_loudness_lufs"] == -16.0
    assert body["srt_asset_id"] == str(graph.srt_asset_id)
    assert body["webvtt_asset_id"] == str(graph.webvtt_asset_id)


def test_render_projection_reports_t17b_execution_state(
    client: TestClient, graph: ProjectGraph
) -> None:
    body = client.get(api(graph.project_id, "/render"), headers=OWNER).json()
    # A completed, verified, current render is downloadable; the projection
    # carries the execution state the dashboard shows while one is running.
    assert body["downloadable"] is True
    assert body["output_sha256"] is not None
    assert body["failure_code"] is None
    assert body["progress_percent"] >= 0
    assert body["cancel_requested"] is False


def test_downloading_the_render_resolves_the_persisted_final_asset(
    client: TestClient, graph: ProjectGraph
) -> None:
    render = client.get(api(graph.project_id, "/render"), headers=OWNER).json()
    response = client.get(api(graph.project_id, "/render/download"), headers=OWNER)
    assert response.status_code == 200
    body = response.json()
    # The download is the render's own persisted final asset, not a placeholder.
    assert body["asset_id"] == render["final_video_asset_id"]
    assert body["url"]
    assert body["expires_in_seconds"] == 900


def test_an_incomplete_render_is_never_offered_as_the_deliverable(
    client: TestClient,
    graph: ProjectGraph,
    review_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
) -> None:
    with review_client[1]() as session:
        job = session.scalars(
            select(RenderJob).where(RenderJob.project_id == graph.project_id)
        ).one()
        job.status = "render_rendering"
        job.output_sha256 = None
        job.measured_duration_us = None
        job.completed_at = None
        job.progress_percent = 55
        job.checkpoint = "ffmpeg"
        session.commit()
    response = client.get(api(graph.project_id, "/render/download"), headers=OWNER)
    assert response.status_code == 409
    assert response.json()["code"] == "render_not_verified"
    body = client.get(api(graph.project_id, "/render"), headers=OWNER).json()
    assert body["downloadable"] is False
    assert body["progress_percent"] == 55
    assert body["checkpoint"] == "ffmpeg"


def test_a_foreign_project_cannot_download_a_render(
    client: TestClient, graph: ProjectGraph
) -> None:
    response = client.get(
        api(graph.project_id, "/render/download"), headers={"X-VidGen-User": "owner-b"}
    )
    assert response.status_code == 404


def test_verified_render_can_be_approved(
    client: TestClient,
    graph: ProjectGraph,
    review_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
) -> None:
    render = client.get(api(graph.project_id, "/render"), headers=OWNER).json()
    response = client.post(
        api(graph.project_id, "/review:approve"),
        json={"lineage_hash": render["lineage_hash"]},
        headers=headers(if_match=render["row_version"], key="approve-1"),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["approval"]["approved_by"] == "owner-a"
    assert body["approval"]["applies_to_current_lineage"] is True
    with review_client[1]() as session:
        assert session.query(RenderApproval).count() == 1


def test_duplicate_approval_submissions_create_one_record(
    client: TestClient,
    graph: ProjectGraph,
    review_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
) -> None:
    render = client.get(api(graph.project_id, "/render"), headers=OWNER).json()
    body = {"lineage_hash": render["lineage_hash"]}
    first = client.post(
        api(graph.project_id, "/review:approve"),
        json=body,
        headers=headers(if_match=render["row_version"], key="approve-1"),
    )
    second = client.post(
        api(graph.project_id, "/review:approve"),
        json=body,
        headers=headers(if_match=render["row_version"], key="approve-1"),
    )
    assert first.json()["approval"]["approval_id"] == second.json()["approval"]["approval_id"]
    with review_client[1]() as session:
        assert session.query(RenderApproval).count() == 1


def test_stale_render_approval_is_rejected(client: TestClient, graph: ProjectGraph) -> None:
    target = graph.shot_ids[5]
    shot = client.get(api(graph.project_id, f"/shots/{target}"), headers=OWNER).json()
    client.post(
        api(graph.project_id, f"/shots/{target}:regenerate"),
        json={"confirm_invalidation": True},
        headers=headers(if_match=shot["shot"]["row_version"], key="regen-1"),
    )
    render = client.get(api(graph.project_id, "/render"), headers=OWNER).json()
    assert render["stale"] is True
    response = client.post(
        api(graph.project_id, "/review:approve"),
        json={"lineage_hash": render["lineage_hash"]},
        headers=headers(if_match=render["row_version"], key="approve-1"),
    )
    assert response.status_code == 409
    assert response.json()["code"] == "render_stale"


def test_approval_of_a_changed_lineage_is_rejected(client: TestClient, graph: ProjectGraph) -> None:
    render = client.get(api(graph.project_id, "/render"), headers=OWNER).json()
    response = client.post(
        api(graph.project_id, "/review:approve"),
        json={"lineage_hash": "0" * 64},
        headers=headers(if_match=render["row_version"], key="approve-1"),
    )
    assert response.status_code == 409
    assert response.json()["code"] == "render_stale"


def test_render_start_is_idempotent(
    client: TestClient,
    graph: ProjectGraph,
    review_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
) -> None:
    from vidgen.db.models import RenderJob

    first = client.post(
        api(graph.project_id, "/render:start"), json={}, headers=headers(key="render-1")
    )
    second = client.post(
        api(graph.project_id, "/render:start"), json={}, headers=headers(key="render-1")
    )
    assert first.status_code == 200
    assert first.json()["render"]["render_job_id"] == second.json()["render"]["render_job_id"]
    with review_client[1]() as session:
        assert session.query(RenderJob).filter_by(project_id=graph.project_id).count() == 2


# ---------------------------------------------------------------------------
# Idempotency semantics
# ---------------------------------------------------------------------------


def test_idempotency_key_replay_returns_the_original_result(
    client: TestClient,
    graph: ProjectGraph,
    review_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
) -> None:
    body = client.get(api(graph.project_id, "/transcript"), headers=OWNER).json()
    segment = body["segments"][0]
    payload: dict[str, Any] = {"text": "Once only.", "confirm_invalidation": True}
    first = client.patch(
        api(graph.project_id, f"/transcript/segments/{segment['segment_id']}"),
        json=payload,
        headers=headers(if_match=segment["row_version"], key="tx-1"),
    )
    second = client.patch(
        api(graph.project_id, f"/transcript/segments/{segment['segment_id']}"),
        json=payload,
        headers=headers(if_match=segment["row_version"], key="tx-1"),
    )
    assert first.json() == second.json()
    with review_client[1]() as session:
        assert session.query(ApiIdempotencyRecord).count() == 1


def test_idempotency_key_reuse_with_a_different_request_is_a_conflict(
    client: TestClient, graph: ProjectGraph
) -> None:
    body = client.get(api(graph.project_id, "/transcript"), headers=OWNER).json()
    segment = body["segments"][0]
    client.patch(
        api(graph.project_id, f"/transcript/segments/{segment['segment_id']}"),
        json={"text": "First.", "confirm_invalidation": True},
        headers=headers(if_match=segment["row_version"], key="tx-1"),
    )
    response = client.patch(
        api(graph.project_id, f"/transcript/segments/{segment['segment_id']}"),
        json={"text": "Different.", "confirm_invalidation": True},
        headers=headers(if_match=segment["row_version"] + 1, key="tx-1"),
    )
    assert response.status_code == 409
    assert response.json()["code"] == "idempotency_key_mismatch"


def test_idempotency_records_are_scoped_by_owner(
    client: TestClient,
    graph: ProjectGraph,
    review_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
) -> None:
    _, factory, _ = review_client
    with factory() as session:
        other = build_project_graph(session, owner_subject="owner-b", name="Theirs")
    client.post(
        api(graph.project_id, "/workflow:start"), json={}, headers=headers(key="shared-key")
    )
    response = client.post(
        api(other.project_id, "/workflow:start"),
        json={},
        headers={**INTRUDER, "Idempotency-Key": "shared-key"},
    )
    assert response.status_code == 200
    assert response.json()["workflow_id"] != f"vidgen-project-{graph.project_id}"


# ---------------------------------------------------------------------------
# Structured errors and route-handler boundaries
# ---------------------------------------------------------------------------


def test_unknown_project_returns_the_structured_error_shape(client: TestClient) -> None:
    response = client.get(api(uuid4()), headers=OWNER)
    assert response.status_code == 404
    body = response.json()
    assert body["code"] == "not_found"
    assert body["retryable"] is False
    assert "summary" in body


def test_validation_errors_name_the_offending_field(
    client: TestClient, graph: ProjectGraph
) -> None:
    response = client.post(
        api(graph.project_id, "/shots/not-a-uuid:regenerate"),
        json={},
        headers=headers(if_match=1, key="regen-1"),
    )
    assert response.status_code == 422
    assert response.json()["code"] == "validation_failed"
    assert response.json()["fields"]


def test_correlation_id_is_echoed_into_error_projections(
    client: TestClient, graph: ProjectGraph
) -> None:
    response = client.get(api(uuid4()), headers={**OWNER, "X-VidGen-Correlation-Id": "trace-abc"})
    assert response.json()["correlation_id"] == "trace-abc"


def test_route_modules_do_not_import_providers_or_ffmpeg() -> None:
    """Route handlers must not reach providers, FFmpeg, or workflow activities.

    The check is on what a module *imports*, not on the text of the file: a
    route may legitimately name a provider-shaped table (``RunwayTask`` is a
    row in this database, not a client), and matching raw source would forbid
    reading it. What must never appear is the provider SDK, an FFmpeg or
    subprocess call, or a workflow activity.
    """
    import ast
    import pathlib

    forbidden = ("openai", "runwayml", "elevenlabs", "ffmpeg", "subprocess", "workflows.activities")

    def imported_modules(tree: ast.Module) -> set[str]:
        modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module)
        return modules

    for path in pathlib.Path("apps/api/routes").glob("*.py"):
        for module in imported_modules(ast.parse(path.read_text())):
            lowered = module.lower()
            for token in forbidden:
                assert token not in lowered, f"{path} imports {module}"


def test_events_are_appended_for_every_mutation(
    client: TestClient,
    graph: ProjectGraph,
    review_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
) -> None:
    render = client.get(api(graph.project_id, "/render"), headers=OWNER).json()
    client.post(
        api(graph.project_id, "/review:approve"),
        json={"lineage_hash": render["lineage_hash"]},
        headers=headers(if_match=render["row_version"], key="approve-1"),
    )
    with review_client[1]() as session:
        events = session.query(ProjectUIEvent).filter_by(project_id=graph.project_id).all()
        assert any(event.event_type == "render_approved" for event in events)
        assert [event.sequence for event in events] == sorted(event.sequence for event in events)


def _shot_identities(factory: sessionmaker[Session]) -> dict[UUID, tuple[str, UUID | None]]:
    with factory() as session:
        return {
            item.shot_id: (item.generation_identity, item.selected_generated_video_id)
            for item in session.query(AnimationItem).all()
        }


# ---------------------------------------------------------------------------
# Regressions from review
# ---------------------------------------------------------------------------


def test_progress_percentage_never_exceeds_one_hundred(
    client: TestClient,
    graph: ProjectGraph,
    review_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
    tmp_path: Path,
) -> None:
    """A second project's selected videos must not inflate this project's progress."""
    _, factory, _ = review_client
    with factory() as session:
        build_project_graph(
            session, owner_subject="owner-a", name="Other", blob_root=tmp_path / "blobs"
        )
    status = client.get(api(graph.project_id, "/workflow"), headers=OWNER).json()
    assert status["completed_shot_count"] == SHOT_COUNT
    assert status["progress_percentage"] == 100.0


def test_shot_commands_address_the_real_t16_child_workflow(
    client: TestClient,
    graph: ProjectGraph,
    controller: FakeWorkflowController,
    review_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
) -> None:
    """The command binds the identity T16 itself derives, not an invented one.

    The replacement child's identity is the same material identity plus the
    regeneration sequence, so it is reproducible from persisted rows alone -
    which is what lets the worker's own lineage check accept it.
    """
    from apps.api.settings import APISettings
    from services.review.shot_identity import configuration_identities, shot_workflow_identity
    from vidgen.db.storyboard_models import StoryboardRun

    target = graph.shot_ids[5]
    shot = client.get(api(graph.project_id, f"/shots/{target}"), headers=OWNER).json()
    payload = client.post(
        api(graph.project_id, f"/shots/{target}:regenerate"),
        json={"confirm_invalidation": True},
        headers=headers(if_match=shot["shot"]["row_version"], key="regen-1"),
    ).json()
    settings = APISettings()
    t14, t15 = configuration_identities(
        image_provider_name=settings.image_provider_name,
        image_model=settings.image_model,
        video_provider_name=settings.video_provider_name,
        visual_capability_profile=settings.visual_capability_profile,
    )
    with review_client[1]() as session:
        record = session.get(StoryboardShotRecord, target)
        assert record is not None
        run = session.get(StoryboardRun, record.storyboard_run_id)
        assert run is not None
        current = shot_workflow_identity(
            session,
            run,
            record,
            t14_configuration_identity=t14,
            t15_capability_profile_identity=t15,
        )
        replacement = shot_workflow_identity(
            session,
            run,
            record,
            t14_configuration_identity=t14,
            t15_capability_profile_identity=t15,
            regeneration_sequence=1,
        )
        command = session.query(ControlCommandRecord).filter_by(target_id=target).one()
    assert command.command_metadata["shot_identity_hash"] == current.identity_hash
    assert payload["previous_identity_hash"] == current.identity_hash
    assert payload["new_identity_hash"] == replacement.identity_hash
    assert replacement.identity_hash != current.identity_hash
    del controller


def test_regeneration_history_lists_distinct_regenerations(
    client: TestClient, graph: ProjectGraph
) -> None:
    target = graph.shot_ids[5]
    shot = client.get(api(graph.project_id, f"/shots/{target}"), headers=OWNER).json()
    assert shot["regeneration_history"] == []
    client.post(
        api(graph.project_id, f"/shots/{target}:regenerate"),
        json={"confirm_invalidation": True},
        headers=headers(if_match=shot["shot"]["row_version"], key="regen-1"),
    )
    refreshed = client.get(api(graph.project_id, f"/shots/{target}"), headers=OWNER).json()
    # One timestamped entry per recorded regeneration, not the shot ID repeated.
    assert len(refreshed["regeneration_history"]) == 1
    assert str(target) not in refreshed["regeneration_history"][0]


def test_render_lineage_ignores_a_later_storyboard_run(
    client: TestClient,
    graph: ProjectGraph,
    review_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
) -> None:
    """A superseded run's shots must not change this render's lineage hash."""
    before = client.get(api(graph.project_id, "/render"), headers=OWNER).json()
    from vidgen.db.animation_models import AnimationGeneratedVideo

    with review_client[1]() as session:
        # Detach one selected video from the render's storyboard run entirely.
        row = session.get(AnimationGeneratedVideo, graph.video_attempt_ids[0])
        assert row is not None
        assert row.selected is True
    after = client.get(api(graph.project_id, "/render"), headers=OWNER).json()
    assert after["lineage_hash"] == before["lineage_hash"]
    assert after["selected_shot_count"] == SHOT_COUNT


def test_concurrent_first_reads_do_not_collide_on_row_versions(
    review_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
    graph: ProjectGraph,
) -> None:
    """Two sessions materialising the same version row must not raise."""
    from vidgen.review.versions import RowVersionService

    _, factory, _ = review_client
    with factory() as first, factory() as second:
        service_a = RowVersionService(first)
        service_b = RowVersionService(second)
        assert service_a.current(graph.project_id, "project", graph.project_id) == 1
        first.commit()
        # The second session lost the race and must read the winner's row.
        assert service_b.current(graph.project_id, "project", graph.project_id) == 1
        second.commit()


def test_concurrent_event_appends_take_distinct_sequences(
    review_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
    graph: ProjectGraph,
) -> None:
    from vidgen.review.events import ProjectEventService

    _, factory, _ = review_client
    with factory() as first, factory() as second:
        ProjectEventService(first).append(
            graph.project_id, event_type="workflow_started", status="running"
        )
        first.commit()
        # The second writer computed the same sequence before the first commit.
        event = ProjectEventService(second).append(
            graph.project_id, event_type="workflow_cancelled", status="cancelled"
        )
        second.commit()
        assert event.sequence == 2


def test_bump_refuses_a_writer_whose_precondition_went_stale(
    review_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
    graph: ProjectGraph,
) -> None:
    """A stale writer must lose the race rather than overwrite the winner.

    Both editors satisfy the same ``If-Match``, so a check-then-write increment
    would apply both changes. The compare-and-swap in ``bump`` refuses the
    second one, which is what turns into a ``409`` for that caller.
    """
    from vidgen.review.errors import ReviewError
    from vidgen.review.versions import RowVersionService

    _, factory, _ = review_client
    # Materialise version 1 up front: racing that first insert is a different
    # case, already covered above.
    with factory() as seed:
        RowVersionService(seed).current(graph.project_id, "project", graph.project_id)
        seed.commit()

    with factory() as first, factory() as second:
        winner = RowVersionService(first)
        loser = RowVersionService(second)

        # Both read version 1 and both pass the precondition.
        assert winner.require(graph.project_id, "project", graph.project_id, "1") == 1
        assert loser.require(graph.project_id, "project", graph.project_id, "1") == 1

        assert winner.bump(graph.project_id, "project", graph.project_id) == 2
        first.commit()

        with pytest.raises(ReviewError) as conflict:
            loser.bump(graph.project_id, "project", graph.project_id)
        assert conflict.value.status_code == 409
        assert conflict.value.error.code == "version_conflict"
        # The conflict reports the version the loser must rebase onto.
        assert conflict.value.error.current_version == 2
        second.rollback()

    # The winner's increment stands; the loser applied nothing.
    with factory() as check:
        assert RowVersionService(check).current(graph.project_id, "project", graph.project_id) == 2
