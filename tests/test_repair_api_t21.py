"""Owner-scoped T21 control-plane behaviour.

The repair API is read-mostly. The one mutation it exposes records an owner
decision - resume a durable technical operation, cancel before the next paid
attempt, acknowledge or resolve a review, restart after an upstream reference
correction - and none of them can make a hard-failing visual pass.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from services.qa.commands import VisualQACommandOptions, run_visual_qa
from tests.repair_fixtures import failing_profile, identity_resolver
from tests.visual_qa_fixtures import VisualQAFixture, build_visual_qa_project
from vidgen.contracts.visual_qa import VisualQATargetType
from vidgen.db.animation_models import AnimationGeneratedVideo
from vidgen.db.repair_models import RepairAttemptRecord, RepairDecisionRecord, RepairRun
from vidgen.db.visual_qa_repository import VisualQARepository
from vidgen.storage.blob import FilesystemBlobStore

OWNER = {"X-VidGen-User": "owner-a"}
INTRUDER = {"X-VidGen-User": "owner-b"}
WIDTH, HEIGHT = 320, 180


@pytest.fixture
def repair_client(
    review_client: tuple[TestClient, sessionmaker[Session], object], tmp_path: Path
) -> Iterator[tuple[TestClient, sessionmaker[Session], VisualQAFixture, RepairRun]]:
    client, factory, _ = review_client
    blob_root = tmp_path / "blobs"
    store = FilesystemBlobStore(blob_root, b"test-secret")
    with factory() as session:
        fixture = build_visual_qa_project(
            session, blob_root, tmp_path / "media", owner_subject="owner-a", shot_count=1
        )
        asyncio.run(
            run_visual_qa(
                session,
                store,
                project_id=fixture.project_id,
                options=VisualQACommandOptions(
                    provider="fake",
                    fake_defects={fixture.shot_ids[0]: failing_profile()},
                    shot_id=fixture.shot_ids[0],
                    targets=(VisualQATargetType.VIDEO,),
                    expected_width=WIDTH,
                    expected_height=HEIGHT,
                ),
                identity_resolver=identity_resolver,
            )
        )
        run = _seed_repair(session, fixture)
        yield client, factory, fixture, run


def _seed_repair(session: Session, fixture: VisualQAFixture) -> RepairRun:
    """One finished repair run in ``HUMAN_REVIEW_REQUIRED`` with a full lineage."""
    shot_id = fixture.shot_ids[0]
    video = session.scalar(
        select(AnimationGeneratedVideo).where(
            AnimationGeneratedVideo.shot_id == shot_id,
            AnimationGeneratedVideo.selected.is_(True),
        )
    )
    assert video is not None
    repository = VisualQARepository(session)
    qa_run = repository.canonical_run(shot_id, VisualQATargetType.VIDEO)
    assert qa_run is not None
    qa_result = repository.canonical_result(qa_run.id)
    assert qa_result is not None
    run = RepairRun(
        project_id=fixture.project_id,
        shot_id=shot_id,
        root_animation_attempt_id=video.id,
        triggering_qa_result_id=qa_result.id,
        policy_version="t21-repair-policy/1.0",
        policy={
            "policy_version": "t21-repair-policy/1.0",
            "per_shot_repair_cost_limit": None,
        },
        classifier_version="t21-repair-classifier/1.0",
        planner_version="t21-repair-planner-deterministic/1.0",
        input_hash="a" * 64,
        idempotency_key="t21-api:1",
        state="HUMAN_REVIEW_REQUIRED",
        human_review_reason="attempt_limit_reached",
        classification={
            "category": "prompt_issue",
            "severity": "structural",
            "primary_code": "wrong_character_identity",
        },
        total_attempt_count=2,
        same_provider_repairs_used=2,
        alternate_provider_attempts_used=1,
        fallback_renders_used=0,
        total_repair_cost=Decimal("0.600000"),
    )
    session.add(run)
    session.flush()
    root = RepairAttemptRecord(
        repair_run_id=run.id,
        shot_id=shot_id,
        attempt_ordinal=0,
        attempt_kind="original",
        attempt_identity="b" * 64,
        root_animation_attempt_id=video.id,
        provider="fake",
        model="gen4_turbo",
        status="failed",
        output_qa_result_id=qa_result.id,
    )
    session.add(root)
    session.flush()
    session.add(
        RepairAttemptRecord(
            repair_run_id=run.id,
            shot_id=shot_id,
            attempt_ordinal=1,
            attempt_kind="same_provider_repair",
            attempt_identity="c" * 64,
            root_animation_attempt_id=video.id,
            predecessor_attempt_id=root.id,
            provider="fake",
            model="gen4_turbo",
            prompt_hash="d" * 64,
            prompt_delta={
                "planner_version": "t21-repair-planner-deterministic/1.0",
                "repair_reason": "repair wrong_character_identity",
                "added_clauses": ["Match the referenced character exactly."],
                "removed_clauses": [],
                "rewritten_clauses": [],
                "preserved_constraint_ids": ["location", "timing"],
                "touched_constraint_ids": [],
                "before_prompt_hash": "e" * 64,
                "after_prompt_hash": "f" * 64,
                "seed_changed": True,
                "previous_seed": None,
                "new_seed": 42,
            },
            seed=42,
            status="failed",
            estimated_cost=Decimal("0.600000"),
            actual_cost=Decimal("0.600000"),
        )
    )
    session.add(
        RepairDecisionRecord(
            repair_run_id=run.id,
            sequence=0,
            route="human_review_required",
            rationale=["the bounded repair policy is exhausted"],
            human_review_reason="attempt_limit_reached",
            estimated_next_cost=Decimal("0"),
            planner_version="t21-repair-planner-deterministic/1.0",
            policy_version="t21-repair-policy/1.0",
            created_at=datetime.now(UTC),
        )
    )
    session.commit()
    return run


def _shot(fixture: VisualQAFixture) -> UUID:
    return fixture.shot_ids[0]


def test_a_foreign_owner_cannot_see_a_repair(
    repair_client: tuple[TestClient, sessionmaker[Session], VisualQAFixture, RepairRun],
) -> None:
    client, _factory, fixture, run = repair_client
    for headers, expected in ((OWNER, 200), (INTRUDER, 404)):
        response = client.get(f"/api/v1/projects/{fixture.project_id}/repairs", headers=headers)
        assert response.status_code == expected
    response = client.get(
        f"/api/v1/projects/{fixture.project_id}/shots/{_shot(fixture)}/repairs/{run.id}",
        headers=INTRUDER,
    )
    assert response.status_code == 404


def test_a_repair_run_from_another_project_is_indistinguishable_from_a_missing_one(
    repair_client: tuple[TestClient, sessionmaker[Session], VisualQAFixture, RepairRun],
) -> None:
    client, _factory, fixture, _run = repair_client
    response = client.get(
        f"/api/v1/projects/{fixture.project_id}/shots/{_shot(fixture)}/repairs/{uuid4()}",
        headers=OWNER,
    )
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_the_projection_exposes_the_lineage_and_never_a_prompt(
    repair_client: tuple[TestClient, sessionmaker[Session], VisualQAFixture, RepairRun],
) -> None:
    client, _factory, fixture, run = repair_client
    response = client.get(
        f"/api/v1/projects/{fixture.project_id}/shots/{_shot(fixture)}/repairs/{run.id}",
        headers=OWNER,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "HUMAN_REVIEW_REQUIRED"
    assert body["human_review_reason"] == "attempt_limit_reached"
    assert body["failure_category"] == "prompt_issue"
    assert body["repair_code"] == "wrong_character_identity"
    assert body["qa_score"] is not None and body["pass_threshold"] == 85
    assert [item["attempt_ordinal"] for item in body["attempts"]] == [0, 1]
    assert body["attempts"][1]["predecessor_attempt_id"] == body["attempts"][0]["attempt_id"]
    delta = body["attempts"][1]["prompt_delta"]
    assert delta["added_clauses"] == ["Match the referenced character exactly."]
    assert delta["seed_changed"] is True
    # The compact projection carries no prompt, provider payload or signed URL.
    serialized = response.text
    assert 'prompt":' not in serialized.replace("prompt_hash", "").replace("prompt_delta", "")
    assert "Authorization" not in serialized and "https://" not in serialized
    assert body["budget"]["total_repair_cost"] == "0.600000"
    # The ETag publishes the row version a mutation must echo back.
    assert response.headers["ETag"] == f'"{body["row_version"]}"'


def test_an_action_requires_a_precondition_and_an_idempotency_key(
    repair_client: tuple[TestClient, sessionmaker[Session], VisualQAFixture, RepairRun],
) -> None:
    client, _factory, fixture, run = repair_client
    path = f"/api/v1/projects/{fixture.project_id}/shots/{_shot(fixture)}/repairs/{run.id}:act"
    # A missing idempotency key is rejected before anything else happens.
    missing_key = client.post(path, json={"action": "resolve", "reason": ""}, headers=OWNER)
    assert missing_key.status_code in {409, 428}
    assert missing_key.json()["code"] in {
        "idempotency_key_required",
        "precondition_required",
    }
    # So is a missing If-Match precondition.
    missing_precondition = client.post(
        path,
        json={"action": "resolve", "reason": ""},
        headers={**OWNER, "Idempotency-Key": "act-precondition"},
    )
    assert missing_precondition.status_code == 428
    assert missing_precondition.json()["code"] == "precondition_required"


def test_resolving_a_review_is_recorded_and_replayed(
    repair_client: tuple[TestClient, sessionmaker[Session], VisualQAFixture, RepairRun],
) -> None:
    client, factory, fixture, run = repair_client
    path = f"/api/v1/projects/{fixture.project_id}/shots/{_shot(fixture)}/repairs/{run.id}"
    current = client.get(path, headers=OWNER).json()["row_version"]
    headers = {**OWNER, "If-Match": f'"{current}"', "Idempotency-Key": "act-1"}
    first = client.post(
        f"{path}:act", json={"action": "resolve", "reason": "reviewed"}, headers=headers
    )
    assert first.status_code == 200
    assert first.json()["code"] == "human_review_resolved"
    # A replay of the same key returns the same body without acting twice.
    second = client.post(
        f"{path}:act", json={"action": "resolve", "reason": "reviewed"}, headers=headers
    )
    assert second.json() == first.json()
    with factory() as session:
        stored = session.get(RepairRun, run.id)
        assert stored is not None and stored.human_review_resolved_at is not None


def test_a_state_that_does_not_accept_an_action_is_refused(
    repair_client: tuple[TestClient, sessionmaker[Session], VisualQAFixture, RepairRun],
) -> None:
    client, _factory, fixture, run = repair_client
    path = f"/api/v1/projects/{fixture.project_id}/shots/{_shot(fixture)}/repairs/{run.id}"
    current = client.get(path, headers=OWNER).json()["row_version"]
    response = client.post(
        f"{path}:act",
        json={"action": "cancel", "reason": ""},
        headers={**OWNER, "If-Match": f'"{current}"', "Idempotency-Key": "act-2"},
    )
    # A run already waiting for a human is not cancelled before a paid attempt.
    assert response.status_code == 409
    assert "does not accept cancel" in response.json()["summary"]


def test_no_owner_action_can_mark_a_hard_failing_visual_as_passed(
    repair_client: tuple[TestClient, sessionmaker[Session], VisualQAFixture, RepairRun],
) -> None:
    """Selection is earned by a new valid T20 result, never by an API call."""
    client, factory, fixture, run = repair_client
    path = f"/api/v1/projects/{fixture.project_id}/shots/{_shot(fixture)}/repairs/{run.id}"
    current = client.get(path, headers=OWNER).json()["row_version"]
    response = client.post(
        f"{path}:act",
        json={"action": "resolve", "reason": "looks fine to me"},
        headers={**OWNER, "If-Match": f'"{current}"', "Idempotency-Key": "act-3"},
    )
    assert response.status_code == 200
    with factory() as session:
        stored = session.get(RepairRun, run.id)
        assert stored is not None
        # Acknowledging a review never selects an attempt or locks the shot.
        assert stored.state != "LOCKED"
        assert stored.selected_attempt_id is None
        assert stored.final_qa_result_id is None
        assert (
            session.scalars(
                select(RepairAttemptRecord).where(RepairAttemptRecord.selected.is_(True))
            ).all()
            == []
        )


def test_restarting_after_an_upstream_correction_reopens_the_run(
    repair_client: tuple[TestClient, sessionmaker[Session], VisualQAFixture, RepairRun],
) -> None:
    client, factory, fixture, run = repair_client
    path = f"/api/v1/projects/{fixture.project_id}/shots/{_shot(fixture)}/repairs/{run.id}"
    current = client.get(path, headers=OWNER).json()["row_version"]
    response = client.post(
        f"{path}:act",
        json={"action": "restart_after_reference_correction", "reason": "reference fixed"},
        headers={**OWNER, "If-Match": f'"{current}"', "Idempotency-Key": "act-4"},
    )
    assert response.status_code == 200
    with factory() as session:
        stored = session.get(RepairRun, run.id)
        assert stored is not None
        assert stored.state == "REPAIR_PLANNING"
        assert stored.human_review_reason is None
        # The previous attempts stay as immutable history.
        assert (
            len(
                session.scalars(
                    select(RepairAttemptRecord).where(RepairAttemptRecord.repair_run_id == run.id)
                ).all()
            )
            == 2
        )
