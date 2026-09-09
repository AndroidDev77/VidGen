"""Generation settings through the API: creation, edits, defaults and estimates."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select

from services.generation.estimate import estimate_generation_costs
from tests.client_fixtures import review_client_context
from vidgen.contracts.generation import GenerationCostEstimate, ShotPacing
from vidgen.db.models import Project

OWNER = {"X-VidGen-User": "owner-a"}


def _create(client, **overrides):
    body = {
        "name": "Recap",
        "target_duration_seconds": 300,
        "budget_warning_cap": "0",
        "budget_hard_cap": "0",
    }
    body.update(overrides)
    return client.post("/api/v1/projects", json=body, headers=OWNER)


def test_a_new_project_defaults_to_balanced_quality_and_normal_pacing(tmp_path: Path) -> None:
    with review_client_context(tmp_path) as (client, factory, _):
        created = _create(client)
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["generation_quality"] == "balanced"
        assert body["shot_pacing"] == "normal"
        assert body["premium_fallback_allowed"] is False
        with factory() as session:
            project = session.get(Project, UUID(body["id"]))
            assert project is not None
            stored = project.settings["generation"]
            assert stored["generation_quality"] == "balanced"
            assert stored["origin"] == "explicit"
            assert stored["settings_version"] == "generation-settings/1"


def test_creation_accepts_explicit_strict_values(tmp_path: Path) -> None:
    with review_client_context(tmp_path) as (client, _, _):
        created = _create(
            client,
            generation_quality="premium",
            shot_pacing="fast",
            premium_fallback_allowed=True,
        )
        assert created.status_code == 201, created.text
        fetched = client.get(f"/api/v1/projects/{created.json()['id']}", headers=OWNER).json()
        assert fetched["generation_quality"] == "premium"
        assert fetched["shot_pacing"] == "fast"
        assert fetched["premium_fallback_allowed"] is True


@pytest.mark.parametrize(
    "overrides",
    [
        {"generation_quality": "ultra"},
        {"generation_quality": "Economy"},
        {"shot_pacing": "slow"},
        {"shot_pacing": 3},
        {"premium_fallback_allowed": "yes please"},
    ],
)
def test_creation_rejects_values_outside_the_strict_vocabulary(
    tmp_path: Path, overrides: dict[str, object]
) -> None:
    with review_client_context(tmp_path) as (client, _, _):
        response = _create(client, **overrides)
        assert response.status_code == 422, response.text


def test_an_existing_project_without_settings_resolves_to_economy(tmp_path: Path) -> None:
    with review_client_context(tmp_path) as (client, factory, _):
        with factory() as session:
            project = Project(
                name="legacy",
                owner_subject="owner-a",
                status="awaiting_upload",
                target_duration_seconds=240,
                visual_style="flat",
                humor_intensity=5,
                settings={},
            )
            session.add(project)
            session.commit()
            project_id = str(project.id)
        fetched = client.get(f"/api/v1/projects/{project_id}", headers=OWNER)
        assert fetched.status_code == 200
        assert fetched.json()["generation_quality"] == "economy"
        assert fetched.json()["shot_pacing"] == "normal"
        settings = client.get(
            f"/api/v1/projects/{project_id}/generation-settings", headers=OWNER
        ).json()
        assert settings["settings"]["origin"] == "legacy_default"
        assert settings["workflow_started"] is False
        assert settings["generation_policy_identity"].startswith("gq=economy;sp=normal;")
        # A legacy hero profile meant the hero-only upgrade balanced now means.
        with factory() as session:
            row = session.scalar(select(Project).where(Project.id == project.id))
            assert row is not None
            row.settings = {"quality_profile": "hero"}
            session.commit()
        fetched = client.get(f"/api/v1/projects/{project_id}", headers=OWNER)
        assert fetched.json()["generation_quality"] == "balanced"


def test_settings_can_be_replaced_and_the_identity_moves_with_them(tmp_path: Path) -> None:
    with review_client_context(tmp_path) as (client, _, _):
        project_id = _create(client).json()["id"]
        before = client.get(f"/api/v1/projects/{project_id}/generation-settings", headers=OWNER)
        assert before.status_code == 200
        updated = client.put(
            f"/api/v1/projects/{project_id}/generation-settings",
            json={"generation_quality": "premium", "shot_pacing": "relaxed"},
            headers=OWNER,
        )
        assert updated.status_code == 200, updated.text
        body = updated.json()
        assert body["settings"]["generation_quality"] == "premium"
        assert body["settings"]["shot_pacing"] == "relaxed"
        assert body["settings"]["premium_fallback_allowed"] is False
        assert body["generation_policy_identity"] != before.json()["generation_policy_identity"]
        assert body["estimate"]["shot_pacing"] == "relaxed"
        # Partial writes and unknown fields are refused.
        assert (
            client.put(
                f"/api/v1/projects/{project_id}/generation-settings",
                json={"generation_quality": "economy"},
                headers=OWNER,
            ).status_code
            == 422
        )
        assert (
            client.put(
                f"/api/v1/projects/{project_id}/generation-settings",
                json={"generation_quality": "economy", "shot_pacing": "fast", "model": "gen4.5"},
                headers=OWNER,
            ).status_code
            == 422
        )
        # Another owner cannot read or write them.
        other = {"X-VidGen-User": "owner-b"}
        assert (
            client.get(
                f"/api/v1/projects/{project_id}/generation-settings", headers=other
            ).status_code
            == 404
        )


def test_the_pre_creation_estimate_orders_the_modes_and_needs_no_project(tmp_path: Path) -> None:
    with review_client_context(tmp_path) as (client, _, _):
        response = client.post(
            "/api/v1/projects/generation-estimate",
            json={"target_duration_seconds": 300, "shot_pacing": "normal"},
            headers=OWNER,
        )
        assert response.status_code == 200, response.text
        estimate = GenerationCostEstimate.model_validate(response.json())
        economy, balanced, premium = estimate.modes
        assert [mode.generation_quality.value for mode in estimate.modes] == [
            "economy",
            "balanced",
            "premium",
        ]
        assert Decimal(economy.estimated_low) < Decimal(balanced.estimated_low)
        assert Decimal(balanced.estimated_low) < Decimal(premium.estimated_low)
        assert Decimal(economy.estimated_low) <= Decimal(economy.estimated_high)
        assert economy.delta_from_economy_low == "0.00"
        assert Decimal(premium.delta_from_economy_high) > Decimal(balanced.delta_from_economy_high)
        assert estimate == estimate_generation_costs(
            target_duration_seconds=300, shot_pacing=ShotPacing.NORMAL
        )
        assert (
            client.post(
                "/api/v1/projects/generation-estimate",
                json={"target_duration_seconds": 0, "shot_pacing": "normal"},
                headers=OWNER,
            ).status_code
            == 422
        )


def test_the_estimate_reflects_pacing_and_is_deterministic() -> None:
    relaxed = estimate_generation_costs(target_duration_seconds=300, shot_pacing=ShotPacing.RELAXED)
    fast = estimate_generation_costs(target_duration_seconds=300, shot_pacing=ShotPacing.FAST)
    assert relaxed.estimated_shot_count_high < fast.estimated_shot_count_high
    assert relaxed == estimate_generation_costs(
        target_duration_seconds=300, shot_pacing=ShotPacing.RELAXED
    )
    # Fewer whole-second round-ups make relaxed pacing the cheaper edit.
    assert Decimal(relaxed.modes[0].estimated_high) <= Decimal(fast.modes[0].estimated_high)
    with pytest.raises(ValueError):
        estimate_generation_costs(target_duration_seconds=0, shot_pacing=ShotPacing.NORMAL)
