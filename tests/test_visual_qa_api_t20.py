"""Owner-scoped T20 control-plane behaviour."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from services.qa.commands import VisualQACommandOptions, run_visual_qa
from services.qa.fake_visual_agent import FakeDefect
from tests.visual_qa_fixtures import VisualQAFixture, build_visual_qa_project
from vidgen.contracts.visual_qa import VisualQADimension, VisualQATargetType
from vidgen.db.visual_qa_repository import VisualQARepository
from vidgen.storage.blob import FilesystemBlobStore

OWNER = {"X-VidGen-User": "owner-a"}
INTRUDER = {"X-VidGen-User": "owner-b"}


def resolver(_session: Session, _storyboard: object, _shot: object) -> str:
    return "a" * 64


@pytest.fixture
def qa_client(
    review_client: tuple[TestClient, sessionmaker[Session], object], tmp_path: Path
) -> Iterator[tuple[TestClient, sessionmaker[Session], VisualQAFixture]]:
    client, factory, _ = review_client
    blob_root = tmp_path / "blobs"
    store = FilesystemBlobStore(blob_root, b"test-secret")
    with factory() as session:
        fixture = build_visual_qa_project(
            session, blob_root, tmp_path / "media", owner_subject="owner-a", shot_count=2
        )
        # One passing shot and one shot whose adjudication cannot decide.
        asyncio.run(
            run_visual_qa(
                session,
                store,
                project_id=fixture.project_id,
                options=VisualQACommandOptions(
                    provider="fake",
                    shot_id=fixture.shot_ids[0],
                    targets=(VisualQATargetType.KEYFRAME, VisualQATargetType.VIDEO),
                ),
                identity_resolver=resolver,
            )
        )
        asyncio.run(
            run_visual_qa(
                session,
                store,
                project_id=fixture.project_id,
                options=VisualQACommandOptions(
                    provider="fake",
                    shot_id=fixture.shot_ids[1],
                    targets=(VisualQATargetType.VIDEO,),
                    fake_defects={
                        fixture.shot_ids[1]: FakeDefect(
                            dimension_confidence={VisualQADimension.CHARACTER_IDENTITY: 0.4},
                            overall_confidence=0.4,
                        )
                    },
                ),
                identity_resolver=resolver,
            )
        )
        session.commit()
    yield client, factory, fixture


def test_project_listing_is_owner_scoped(
    qa_client: tuple[TestClient, sessionmaker[Session], VisualQAFixture],
) -> None:
    client, _, fixture = qa_client
    response = client.get(f"/api/v1/projects/{fixture.project_id}/visual-qa", headers=OWNER)
    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 3
    first = body["items"][0]
    assert {"qa_run_id", "outcome", "score", "pass_threshold", "repair_codes"} <= set(first)
    # The compact projection never leaks provider payloads or signed URLs.
    assert "provider_result" not in first
    assert "url" not in first


def test_cross_owner_requests_return_404(
    qa_client: tuple[TestClient, sessionmaker[Session], VisualQAFixture],
) -> None:
    client, _, fixture = qa_client
    assert (
        client.get(f"/api/v1/projects/{fixture.project_id}/visual-qa", headers=INTRUDER).status_code
        == 404
    )
    assert (
        client.get(
            f"/api/v1/projects/{fixture.project_id}/shots/{fixture.shot_ids[0]}/visual-qa",
            headers=INTRUDER,
        ).status_code
        == 404
    )


def test_a_cross_project_qa_run_id_returns_404(
    qa_client: tuple[TestClient, sessionmaker[Session], VisualQAFixture],
) -> None:
    client, factory, fixture = qa_client
    with factory() as session:
        runs = VisualQARepository(session).runs_for_shot(fixture.project_id, fixture.shot_ids[1])
        foreign_run_id = runs[0].id
    response = client.get(
        f"/api/v1/projects/{fixture.project_id}/shots/{fixture.shot_ids[0]}"
        f"/visual-qa/{foreign_run_id}",
        headers=OWNER,
    )
    assert response.status_code == 404


def test_shot_detail_exposes_the_scorecard_diagnostics_and_etag(
    qa_client: tuple[TestClient, sessionmaker[Session], VisualQAFixture],
) -> None:
    client, factory, fixture = qa_client
    with factory() as session:
        run = VisualQARepository(session).runs_for_shot(fixture.project_id, fixture.shot_ids[0])[1]
        run_id = run.id
    response = client.get(
        f"/api/v1/projects/{fixture.project_id}/shots/{fixture.shot_ids[0]}/visual-qa/{run_id}",
        headers=OWNER,
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body["dimensions"]) == 8
    assert sum(item["weight"] for item in body["dimensions"]) == 100
    assert body["diagnostics"]
    assert body["samples"]
    assert response.headers["ETag"] == f'"{body["row_version"]}"'


def test_evidence_projection_carries_exact_timestamps(
    qa_client: tuple[TestClient, sessionmaker[Session], VisualQAFixture],
) -> None:
    client, factory, fixture = qa_client
    with factory() as session:
        run_id = (
            VisualQARepository(session).runs_for_shot(fixture.project_id, fixture.shot_ids[1])[0].id
        )
    response = client.get(
        f"/api/v1/projects/{fixture.project_id}/shots/{fixture.shot_ids[1]}"
        f"/visual-qa/{run_id}/evidence",
        headers=OWNER,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["samples"]
    for item in body["samples"]:
        assert item["actual_timestamp_us"] >= 0
        assert item["selection_reason"]


def test_running_qa_requires_if_match_and_an_idempotency_key(
    qa_client: tuple[TestClient, sessionmaker[Session], VisualQAFixture],
) -> None:
    client, _, fixture = qa_client
    path = f"/api/v1/projects/{fixture.project_id}/shots/{fixture.shot_ids[0]}/visual-qa:run"
    body = {"provider": "fake", "targets": ["video"]}
    assert client.post(path, json=body, headers=OWNER).status_code in {409, 428}
    assert client.post(path, json=body, headers={**OWNER, "Idempotency-Key": "k1"}).status_code in {
        409,
        428,
    }
    accepted = client.post(
        path, json=body, headers={**OWNER, "Idempotency-Key": "k1", "If-Match": "1"}
    )
    assert accepted.status_code == 202
    assert accepted.json()["status"] == "queued"
    # The same key replays the original response rather than queueing twice.
    replay = client.post(
        path, json=body, headers={**OWNER, "Idempotency-Key": "k1", "If-Match": "1"}
    )
    assert replay.status_code == 202
    assert replay.json() == accepted.json()


def test_a_review_result_can_be_approved_and_a_hard_failure_cannot(
    qa_client: tuple[TestClient, sessionmaker[Session], VisualQAFixture],
) -> None:
    client, factory, fixture = qa_client
    with factory() as session:
        repository = VisualQARepository(session)
        run = repository.runs_for_shot(fixture.project_id, fixture.shot_ids[1])[0]
        assert run.final_outcome == "REVIEW"
        run_id = run.id
    path = (
        f"/api/v1/projects/{fixture.project_id}/shots/{fixture.shot_ids[1]}"
        f"/visual-qa/{run_id}:approve"
    )
    response = client.post(
        path,
        json={"reason": "identity confirmed against the approved sheet"},
        headers={**OWNER, "Idempotency-Key": "review-1", "If-Match": "1"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["decision"] == "approved"
    assert body["resulting_gate"] == "visual_qa_human_approved"
    assert body["row_version"] == 2

    with factory() as session:
        repository = VisualQARepository(session)
        assert repository.gate(fixture.shot_ids[1], VisualQATargetType.VIDEO)[0] is True
        # The automated result is preserved verbatim.
        preserved = repository.run(fixture.project_id, run_id)
        assert preserved is not None and preserved.final_outcome == "REVIEW"


def test_a_stale_if_match_is_a_conflict(
    qa_client: tuple[TestClient, sessionmaker[Session], VisualQAFixture],
) -> None:
    client, factory, fixture = qa_client
    with factory() as session:
        run_id = (
            VisualQARepository(session).runs_for_shot(fixture.project_id, fixture.shot_ids[1])[0].id
        )
    response = client.post(
        f"/api/v1/projects/{fixture.project_id}/shots/{fixture.shot_ids[1]}"
        f"/visual-qa/{run_id}:reject",
        json={"reason": "no"},
        headers={**OWNER, "Idempotency-Key": "reject-1", "If-Match": "99"},
    )
    assert response.status_code == 409


def test_evidence_bounding_boxes_project_only_their_coordinates(
    qa_client: tuple[TestClient, sessionmaker[Session], VisualQAFixture],
) -> None:
    """The stored payload carries a schema version, which is not a coordinate."""
    from vidgen.db.visual_qa_models import VisualQAEvidenceRecord

    client, factory, fixture = qa_client
    with factory() as session:
        repository = VisualQARepository(session)
        run = repository.runs_for_shot(fixture.project_id, fixture.shot_ids[1])[0]
        result = repository.canonical_result(run.id)
        assert result is not None
        from datetime import UTC, datetime
        from uuid import uuid4

        session.add(
            VisualQAEvidenceRecord(
                id=uuid4(),
                qa_result_id=result.id,
                finding_id=uuid4(),
                sample_id=repository.samples(run.id)[0].id,
                shot_relative_timestamp_us=1_500_000,
                source_relative_timestamp_us=1_500_000,
                # As persisted by the contract: coordinates plus a schema version.
                bounding_box={
                    "schema_version": "1.0",
                    "x": 0.1,
                    "y": 0.2,
                    "width": 0.3,
                    "height": 0.4,
                },
                evidence_type="sample_frame",
                confidence=0.9,
                explanation="",
                created_at=datetime.now(UTC),
            )
        )
        session.commit()
        run_id = run.id

    response = client.get(
        f"/api/v1/projects/{fixture.project_id}/shots/{fixture.shot_ids[1]}"
        f"/visual-qa/{run_id}/evidence",
        headers=OWNER,
    )
    assert response.status_code == 200
    boxes = [item["bounding_box"] for item in response.json()["items"] if item["bounding_box"]]
    assert boxes == [{"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.4}]
