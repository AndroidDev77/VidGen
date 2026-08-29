"""Owner-scoped T22 control-plane behaviour.

The final-QA API is read-mostly. Its mutations record an owner decision - start
or resume a run, cancel before a paid analysis, resolve an eligible semantic
review, route a confirmed finding to the stage that owns it - and none of them
can make a deterministic hard failure pass.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from services.qa.final_commands import FinalQACommandOptions, run_final_editorial_qa
from services.qa.final_fake_provider import FakeEditorialDefect, FakeEditorialFinding
from tests.final_qa_fixtures import (
    FIXTURE_CONFIGURATION,
    FinalQAFixture,
    build_final_qa_project,
    replace_final_render,
    require_ffmpeg,
)
from vidgen.contracts.final_editorial import (
    FinalEditorialCategory,
    FinalFindingSeverity,
    FinalIssueCode,
)
from vidgen.db.final_editorial_models import FinalEditorialReview, FinalEditorialRun
from vidgen.storage.blob import FilesystemBlobStore

pytestmark = pytest.mark.skipif(not require_ffmpeg(), reason="FFmpeg and ffprobe are required")

OWNER = {"X-VidGen-User": "owner-a"}
INTRUDER = {"X-VidGen-User": "owner-b"}


def borderline_defect(render_identity: str) -> dict[str, FakeEditorialDefect]:
    """One genuinely uncertain semantic finding: the only kind a human may settle."""
    profile = FakeEditorialDefect(
        findings=(
            FakeEditorialFinding(
                category=FinalEditorialCategory.COMPREHENSIBILITY,
                issue_code=FinalIssueCode.INCOMPREHENSIBLE_SEQUENCE,
                severity=FinalFindingSeverity.REVIEW_REQUIRED,
                summary="The jump between shots four and five may confuse a viewer.",
                start_us=9_000_000,
                end_us=12_000_000,
                confidence=0.55,
                sample_index=4,
                shot_index=3,
            ),
        ),
        overall_confidence=0.55,
    )
    return {
        render_identity: FakeEditorialDefect(
            findings=profile.findings, overall_confidence=0.55, adjudication=profile
        )
    }


@pytest.fixture
def final_qa_client(
    review_client: tuple[TestClient, sessionmaker[Session], object], tmp_path: Path
) -> Iterator[tuple[TestClient, sessionmaker[Session], FinalQAFixture, Path]]:
    client, factory, _ = review_client
    blob_root = tmp_path / "blobs"
    with factory() as session:
        fixture = build_final_qa_project(
            session, blob_root, tmp_path / "media", owner_subject="owner-a"
        )
        yield client, factory, fixture, blob_root


def run_final_qa(
    factory: sessionmaker[Session],
    blob_root: Path,
    fixture: FinalQAFixture,
    **overrides: object,
) -> None:
    store = FilesystemBlobStore(blob_root, b"test-secret")
    with factory() as session:
        asyncio.run(
            run_final_editorial_qa(
                session,
                store,
                project_id=fixture.project_id,
                options=FinalQACommandOptions(
                    provider="fake",
                    configuration=FIXTURE_CONFIGURATION,
                    idempotency_key="t22-api",
                    **overrides,  # type: ignore[arg-type]
                ),
            )
        )


def project_version(client: TestClient, project_id: object) -> int:
    response = client.get(f"/api/v1/projects/{project_id}/final-qa/gate", headers=OWNER)
    assert response.status_code == 200
    return int(response.json()["row_version"])


# --- reads -------------------------------------------------------------------
def test_the_collection_lists_the_projects_final_qa_runs(
    final_qa_client: tuple[TestClient, sessionmaker[Session], FinalQAFixture, Path],
) -> None:
    client, factory, fixture, blob_root = final_qa_client
    run_final_qa(factory, blob_root, fixture)
    response = client.get(f"/api/v1/projects/{fixture.project_id}/final-qa", headers=OWNER)
    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["decision"] == "PASS"
    assert item["selected"] is True
    assert item["render_identity"] == fixture.render_identity
    assert item["provider"] == "fake"


def test_the_detail_projection_carries_checks_findings_and_the_timeline(
    final_qa_client: tuple[TestClient, sessionmaker[Session], FinalQAFixture, Path],
) -> None:
    client, factory, fixture, blob_root = final_qa_client
    run_final_qa(
        factory, blob_root, fixture, fake_defects=borderline_defect(fixture.render_identity)
    )
    with factory() as session:
        run = session.scalars(select(FinalEditorialRun)).one()
    response = client.get(f"/api/v1/projects/{fixture.project_id}/final-qa/{run.id}", headers=OWNER)
    assert response.status_code == 200
    assert response.headers["ETag"]
    body = response.json()
    assert body["decision"] == "REVIEW"
    assert body["measurements"]["video_decoded"] is True
    assert body["media_checks"] and body["audio_checks"] and body["caption_checks"]
    assert body["timeline_duration_us"] == fixture.timeline_duration_us
    finding = body["findings"][0]
    # A timeline marker needs an exact range and something to point at.
    assert finding["end_us"] >= finding["start_us"]
    assert finding["evidence"]
    assert finding["remediation_target"] != ""
    # No provider payload, prompt or signed URL crosses the boundary.
    assert "provider_request_id" not in body
    assert "narrative_summary" not in body


def test_the_gate_endpoint_reports_the_backends_own_completion_answer(
    final_qa_client: tuple[TestClient, sessionmaker[Session], FinalQAFixture, Path],
) -> None:
    client, factory, fixture, blob_root = final_qa_client
    before = client.get(
        f"/api/v1/projects/{fixture.project_id}/final-qa/gate", headers=OWNER
    ).json()
    assert before["allowed"] is False and before["reason"] == "final_qa_missing"

    run_final_qa(factory, blob_root, fixture)
    after = client.get(f"/api/v1/projects/{fixture.project_id}/final-qa/gate", headers=OWNER).json()
    assert after["allowed"] is True and after["decision"] == "PASS"


def test_another_owner_sees_the_same_404_as_a_missing_project(
    final_qa_client: tuple[TestClient, sessionmaker[Session], FinalQAFixture, Path],
) -> None:
    client, factory, fixture, blob_root = final_qa_client
    run_final_qa(factory, blob_root, fixture)
    with factory() as session:
        run = session.scalars(select(FinalEditorialRun)).one()
    assert (
        client.get(f"/api/v1/projects/{fixture.project_id}/final-qa", headers=INTRUDER).status_code
        == 404
    )
    assert (
        client.get(
            f"/api/v1/projects/{fixture.project_id}/final-qa/{run.id}", headers=INTRUDER
        ).status_code
        == 404
    )
    assert (
        client.get(
            f"/api/v1/projects/{fixture.project_id}/final-qa/gate", headers=INTRUDER
        ).status_code
        == 404
    )


def test_a_run_from_another_project_is_not_reachable_through_this_one(
    final_qa_client: tuple[TestClient, sessionmaker[Session], FinalQAFixture, Path],
) -> None:
    client, factory, fixture, blob_root = final_qa_client
    run_final_qa(factory, blob_root, fixture)
    response = client.get(
        f"/api/v1/projects/{fixture.project_id}/final-qa/{uuid4()}", headers=OWNER
    )
    assert response.status_code == 404


# --- mutations ---------------------------------------------------------------
def test_running_final_qa_requires_if_match_and_replays_on_the_same_key(
    final_qa_client: tuple[TestClient, sessionmaker[Session], FinalQAFixture, Path],
) -> None:
    client, _factory, fixture, _blob_root = final_qa_client
    url = f"/api/v1/projects/{fixture.project_id}/final-qa:run"
    body = {"provider": "fake", "adjudicate": True}

    missing = client.post(url, json=body, headers={**OWNER, "Idempotency-Key": "k1"})
    assert missing.status_code == 409

    version = project_version(client, fixture.project_id)
    headers = {**OWNER, "If-Match": f'"{version}"', "Idempotency-Key": "k1"}
    first = client.post(url, json=body, headers=headers)
    assert first.status_code == 202
    replay = client.post(url, json=body, headers=headers)
    assert replay.status_code == 202
    assert replay.json() == first.json()


def test_a_stale_if_match_is_rejected(
    final_qa_client: tuple[TestClient, sessionmaker[Session], FinalQAFixture, Path],
) -> None:
    client, _factory, fixture, _blob_root = final_qa_client
    url = f"/api/v1/projects/{fixture.project_id}/final-qa:run"
    response = client.post(
        url,
        json={"provider": "fake", "adjudicate": True},
        headers={**OWNER, "If-Match": '"9999"', "Idempotency-Key": "k2"},
    )
    assert response.status_code == 409


def test_an_eligible_semantic_review_can_be_resolved_and_the_gate_recomputes(
    final_qa_client: tuple[TestClient, sessionmaker[Session], FinalQAFixture, Path],
) -> None:
    client, factory, fixture, blob_root = final_qa_client
    run_final_qa(
        factory, blob_root, fixture, fake_defects=borderline_defect(fixture.render_identity)
    )
    with factory() as session:
        run = session.scalars(select(FinalEditorialRun)).one()
    detail = client.get(
        f"/api/v1/projects/{fixture.project_id}/final-qa/{run.id}", headers=OWNER
    ).json()
    assert detail["decision"] == "REVIEW"
    finding_id = next(
        item["finding_id"] for item in detail["findings"] if item["severity"] == "review_required"
    )
    version = project_version(client, fixture.project_id)
    response = client.post(
        f"/api/v1/projects/{fixture.project_id}/final-qa/{run.id}:review",
        json={
            "finding_id": finding_id,
            "decision": "accept",
            "reason_code": "reviewer_accepted",
            "reason": "The jump reads clearly enough in context.",
        },
        headers={**OWNER, "If-Match": f'"{version}"', "Idempotency-Key": "review-1"},
    )
    assert response.status_code == 200
    assert response.json()["resulting_gate"] == "PASS"

    with factory() as session:
        review = session.scalars(select(FinalEditorialReview)).one()
        assert review.reviewer_subject == "owner-a"
        assert review.reason
        assert review.expected_row_version == version
    gate = client.get(f"/api/v1/projects/{fixture.project_id}/final-qa/gate", headers=OWNER).json()
    assert gate["allowed"] is True


def test_the_same_finding_cannot_be_adjudicated_twice(
    final_qa_client: tuple[TestClient, sessionmaker[Session], FinalQAFixture, Path],
) -> None:
    client, factory, fixture, blob_root = final_qa_client
    run_final_qa(
        factory, blob_root, fixture, fake_defects=borderline_defect(fixture.render_identity)
    )
    with factory() as session:
        run = session.scalars(select(FinalEditorialRun)).one()
    detail = client.get(
        f"/api/v1/projects/{fixture.project_id}/final-qa/{run.id}", headers=OWNER
    ).json()
    finding_id = detail["findings"][0]["finding_id"]
    body = {
        "finding_id": finding_id,
        "decision": "accept",
        "reason_code": "reviewer_accepted",
        "reason": "Acceptable in context.",
    }
    version = project_version(client, fixture.project_id)
    first = client.post(
        f"/api/v1/projects/{fixture.project_id}/final-qa/{run.id}:review",
        json=body,
        headers={**OWNER, "If-Match": f'"{version}"', "Idempotency-Key": "review-a"},
    )
    assert first.status_code == 200
    version = project_version(client, fixture.project_id)
    second = client.post(
        f"/api/v1/projects/{fixture.project_id}/final-qa/{run.id}:review",
        json={**body, "decision": "reject"},
        headers={**OWNER, "If-Match": f'"{version}"', "Idempotency-Key": "review-b"},
    )
    assert second.status_code == 409


def test_a_deterministic_hard_failure_can_never_be_resolved_by_a_reviewer(
    final_qa_client: tuple[TestClient, sessionmaker[Session], FinalQAFixture, Path],
    tmp_path: Path,
) -> None:
    client, factory, fixture, blob_root = final_qa_client
    corrupt = tmp_path / "corrupt.mp4"
    payload = bytearray((fixture.workspace / "final.mp4").read_bytes())
    for offset in range(len(payload) // 3, (len(payload) * 2) // 3, 7):
        payload[offset] = payload[offset] ^ 0xFF
    corrupt.write_bytes(bytes(payload))
    with factory() as session:
        replace_final_render(session, blob_root, fixture, corrupt)
    run_final_qa(factory, blob_root, fixture)

    with factory() as session:
        run = session.scalars(select(FinalEditorialRun)).one()
        assert run.deterministic_failure_count > 0
    detail = client.get(
        f"/api/v1/projects/{fixture.project_id}/final-qa/{run.id}", headers=OWNER
    ).json()
    assert detail["decision"] == "FAIL"
    version = project_version(client, fixture.project_id)
    response = client.post(
        f"/api/v1/projects/{fixture.project_id}/final-qa/{run.id}:review",
        json={
            "finding_id": detail["findings"][0]["finding_id"],
            "decision": "accept",
            "reason_code": "looks_fine",
            "reason": "I watched it and it seemed fine.",
        },
        headers={**OWNER, "If-Match": f'"{version}"', "Idempotency-Key": "override"},
    )
    assert response.status_code == 409
    assert "deterministic hard failure" in response.text
    gate = client.get(f"/api/v1/projects/{fixture.project_id}/final-qa/gate", headers=OWNER).json()
    assert gate["allowed"] is False


def test_a_remediation_route_may_only_reference_findings_from_this_report(
    final_qa_client: tuple[TestClient, sessionmaker[Session], FinalQAFixture, Path],
) -> None:
    client, factory, fixture, blob_root = final_qa_client
    run_final_qa(
        factory, blob_root, fixture, fake_defects=borderline_defect(fixture.render_identity)
    )
    with factory() as session:
        run = session.scalars(select(FinalEditorialRun)).one()
    detail = client.get(
        f"/api/v1/projects/{fixture.project_id}/final-qa/{run.id}", headers=OWNER
    ).json()
    version = project_version(client, fixture.project_id)
    unknown = client.post(
        f"/api/v1/projects/{fixture.project_id}/final-qa/{run.id}:remediate",
        json={"target": "RERENDER_T17", "finding_ids": [str(uuid4())]},
        headers={**OWNER, "If-Match": f'"{version}"', "Idempotency-Key": "route-1"},
    )
    assert unknown.status_code == 409

    accepted = client.post(
        f"/api/v1/projects/{fixture.project_id}/final-qa/{run.id}:remediate",
        json={
            "target": "RERENDER_T17",
            "finding_ids": [detail["findings"][0]["finding_id"]],
        },
        headers={**OWNER, "If-Match": f'"{version}"', "Idempotency-Key": "route-2"},
    )
    assert accepted.status_code == 202
    # A route that changes a selected input demands a new render and a new run.
    assert accepted.json()["requires_new_render"] is True


def test_an_unknown_remediation_target_is_rejected(
    final_qa_client: tuple[TestClient, sessionmaker[Session], FinalQAFixture, Path],
) -> None:
    client, factory, fixture, blob_root = final_qa_client
    run_final_qa(factory, blob_root, fixture)
    with factory() as session:
        run = session.scalars(select(FinalEditorialRun)).one()
    version = project_version(client, fixture.project_id)
    response = client.post(
        f"/api/v1/projects/{fixture.project_id}/final-qa/{run.id}:remediate",
        json={"target": "MARK_AS_PASSED", "finding_ids": [str(uuid4())]},
        headers={**OWNER, "If-Match": f'"{version}"', "Idempotency-Key": "route-3"},
    )
    assert response.status_code == 409


def test_a_completed_run_cannot_be_cancelled_after_its_analysis(
    final_qa_client: tuple[TestClient, sessionmaker[Session], FinalQAFixture, Path],
) -> None:
    client, factory, fixture, blob_root = final_qa_client
    run_final_qa(factory, blob_root, fixture)
    with factory() as session:
        run = session.scalars(select(FinalEditorialRun)).one()
    version = project_version(client, fixture.project_id)
    response = client.post(
        f"/api/v1/projects/{fixture.project_id}/final-qa/{run.id}:cancel",
        json={"reason": "changed my mind"},
        headers={**OWNER, "If-Match": f'"{version}"', "Idempotency-Key": "cancel-1"},
    )
    assert response.status_code == 409


def test_the_api_exposes_no_action_that_marks_a_deterministic_failure_as_passed() -> None:
    """A structural guard: no route may offer an override affordance."""
    from apps.api.main import create_app

    paths = list(create_app().openapi()["paths"])
    final_paths = [path for path in paths if "final-qa" in path]
    assert final_paths, "the final-QA routes must be registered"
    for path in final_paths:
        assert not any(
            token in path.lower() for token in ("override", "force", "approve", "pass")
        ), path
