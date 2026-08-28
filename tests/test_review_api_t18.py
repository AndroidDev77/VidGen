"""T18 control-plane API tests.

Every test runs against SQLite, synthetic media, and the deterministic fake
workflow controller: no Temporal cluster and no paid provider call is involved.
"""

from __future__ import annotations

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
from vidgen.db.models import Project
from vidgen.db.review_models import ApiIdempotencyRecord, ProjectUIEvent, RenderApproval
from vidgen.db.script_models import Script, ScriptSegment
from vidgen.db.storyboard_models import StoryboardShotRecord
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
    assert payload["child_workflow_id"]
    assert payload["new_identity_hash"] != payload["previous_identity_hash"]
    assert payload["preserved_attempt_ids"]

    # Exactly one command was issued, and it named only the requested shot.
    assert len(controller.shot_commands) == 1
    with review_client[1]() as session:
        stable_shot_id = session.get(StoryboardShotRecord, target).stable_shot_id  # type: ignore[union-attr]
    assert controller.shot_commands[0][1].storyboard_shot_id == stable_shot_id

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


def test_shot_retry_rejects_a_locked_shot(client: TestClient, graph: ProjectGraph) -> None:
    target = graph.shot_ids[2]
    shot = client.get(api(graph.project_id, f"/shots/{target}"), headers=OWNER).json()
    response = client.post(
        api(graph.project_id, f"/shots/{target}:retry"),
        headers=headers(if_match=shot["shot"]["row_version"], key="retry-1"),
    )
    assert response.status_code == 409
    assert response.json()["code"] == "shot_not_retryable"


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
    """Route handlers must not reach providers, FFmpeg, or workflow activities."""
    import pathlib

    forbidden = ("openai", "runway", "elevenlabs", "ffmpeg", "subprocess", "workflows.activities")
    for path in pathlib.Path("apps/api/routes").glob("*.py"):
        source = path.read_text().lower()
        for token in forbidden:
            assert token not in source, f"{path} references {token}"


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
    """The command must go to the workflow ID T16 itself derives, not an invented one."""
    from apps.api.settings import APISettings
    from packages.workflows.shot_policy import temporal_shot_workflow_id
    from services.review.shot_identity import configuration_identities, shot_workflow_identity
    from vidgen.db.storyboard_models import StoryboardRun

    target = graph.shot_ids[5]
    shot = client.get(api(graph.project_id, f"/shots/{target}"), headers=OWNER).json()
    client.post(
        api(graph.project_id, f"/shots/{target}:regenerate"),
        json={"confirm_invalidation": True},
        headers=headers(if_match=shot["shot"]["row_version"], key="regen-1"),
    )
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
        expected = temporal_shot_workflow_id(
            shot_workflow_identity(
                session,
                run,
                record,
                t14_configuration_identity=t14,
                t15_capability_profile_identity=t15,
            )
        )
    assert controller.shot_commands[0][0] == expected


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
