"""Generation settings through the API: creation, edits, defaults and estimates."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import get_args
from uuid import UUID

import pytest
from sqlalchemy import select

from services.generation.estimate import estimate_generation_costs
from services.generation.settings import (
    effective_script_warn_only_validation_codes,
    effective_warn_only_validation_codes,
)
from tests.client_fixtures import review_client_context
from vidgen.contracts.generation import (
    GenerationCostEstimate,
    ProjectGenerationSettings,
    ShotPacing,
)
from vidgen.contracts.script import PLOT_PLAN_VALIDATION_CODES, RECAP_SCRIPT_VALIDATION_CODES
from vidgen.contracts.storyboard import (
    STORYBOARD_WARN_ONLY_ELIGIBLE_VALIDATION_CODES,
    StoryboardValidationCode,
)
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
            json={
                "generation_quality": "premium",
                "shot_pacing": "relaxed",
                "premium_fallback_allowed": False,
            },
            headers=OWNER,
        )
        assert updated.status_code == 200, updated.text
        body = updated.json()
        assert body["settings"]["generation_quality"] == "premium"
        assert body["settings"]["shot_pacing"] == "relaxed"
        assert body["settings"]["premium_fallback_allowed"] is False
        assert body["generation_policy_identity"] != before.json()["generation_policy_identity"]
        assert body["estimate"]["shot_pacing"] == "relaxed"
        # Partial writes and unknown fields are refused: omitting the fallback
        # flag must never silently reset a stored value.
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
                json={"generation_quality": "premium", "shot_pacing": "relaxed"},
                headers=OWNER,
            ).status_code
            == 422
        )
        assert (
            client.put(
                f"/api/v1/projects/{project_id}/generation-settings",
                json={
                    "generation_quality": "economy",
                    "shot_pacing": "fast",
                    "premium_fallback_allowed": False,
                    "model": "gen4.5",
                },
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


def test_the_default_estimate_matches_the_web_fixtures_verbatim() -> None:
    """The web test fixtures and the e2e fake API quote these exact figures."""
    estimate = estimate_generation_costs(target_duration_seconds=300, shot_pacing=ShotPacing.NORMAL)
    assert (estimate.estimated_shot_count_low, estimate.estimated_shot_count_high) == (43, 75)
    assert (estimate.generated_seconds_low, estimate.generated_seconds_high) == (301, 301)
    assert [
        (m.estimated_low, m.estimated_high, m.delta_from_economy_low, m.delta_from_economy_high)
        for m in estimate.modes
    ] == [
        ("15.05", "18.06", "0.00", "0.00"),
        ("18.21", "21.85", "3.16", "3.79"),
        ("36.12", "43.34", "21.07", "25.28"),
    ]


def test_warn_only_validation_codes_default_to_the_deployment_setting(tmp_path: Path) -> None:
    """No override stored: the response reports the deployment default in effect."""
    with review_client_context(tmp_path) as (client, _, _):
        project_id = _create(client).json()["id"]
        body = client.get(
            f"/api/v1/projects/{project_id}/generation-settings", headers=OWNER
        ).json()
        assert body["settings"]["warn_only_validation_codes"] is None
        assert body["effective_warn_only_validation_codes"] == ["SCENE_SET_MISMATCH"]
        assert "UNSUPPORTED_ALIAS_MERGE" in body["available_warn_only_validation_codes"]


def test_warn_only_validation_codes_can_be_overridden_per_project(tmp_path: Path) -> None:
    with review_client_context(tmp_path) as (client, _, _):
        project_id = _create(client).json()["id"]
        updated = client.put(
            f"/api/v1/projects/{project_id}/generation-settings",
            json={
                "generation_quality": "balanced",
                "shot_pacing": "normal",
                "premium_fallback_allowed": False,
                "warn_only_validation_codes": ["DUPLICATE_ID", "SCENE_SET_MISMATCH"],
            },
            headers=OWNER,
        )
        assert updated.status_code == 200, updated.text
        body = updated.json()
        assert body["settings"]["warn_only_validation_codes"] == [
            "DUPLICATE_ID",
            "SCENE_SET_MISMATCH",
        ]
        assert body["effective_warn_only_validation_codes"] == [
            "DUPLICATE_ID",
            "SCENE_SET_MISMATCH",
        ]
        # An empty list is a real choice - tolerate nothing - not "use the default".
        emptied = client.put(
            f"/api/v1/projects/{project_id}/generation-settings",
            json={
                "generation_quality": "balanced",
                "shot_pacing": "normal",
                "premium_fallback_allowed": False,
                "warn_only_validation_codes": [],
            },
            headers=OWNER,
        )
        assert emptied.status_code == 200, emptied.text
        assert emptied.json()["effective_warn_only_validation_codes"] == []


def test_an_unknown_warn_only_validation_code_is_refused(tmp_path: Path) -> None:
    with review_client_context(tmp_path) as (client, _, _):
        response = _create(client, warn_only_validation_codes=["NOT_A_CODE"])
        assert response.status_code == 422, response.text


def test_the_resolver_prefers_the_project_override_over_the_deployment_default() -> None:
    """What the analysis pipeline is handed, for each of the three cases."""
    default = ["SCENE_SET_MISMATCH"]
    assert effective_warn_only_validation_codes(ProjectGenerationSettings(), default) == frozenset(
        {"SCENE_SET_MISMATCH"}
    )
    assert effective_warn_only_validation_codes(
        ProjectGenerationSettings(warn_only_validation_codes=["DUPLICATE_ID"]), default
    ) == frozenset({"DUPLICATE_ID"})
    assert (
        effective_warn_only_validation_codes(
            ProjectGenerationSettings(warn_only_validation_codes=[]), default
        )
        == frozenset()
    )


def test_script_warn_only_validation_codes_default_to_the_deployment_setting(
    tmp_path: Path,
) -> None:
    with review_client_context(tmp_path) as (client, _, _):
        project_id = _create(client).json()["id"]
        body = client.get(
            f"/api/v1/projects/{project_id}/generation-settings", headers=OWNER
        ).json()
        assert body["settings"]["script_warn_only_validation_codes"] is None
        assert body["effective_script_warn_only_validation_codes"] == ["UNKNOWN_SOURCE_REFERENCE"]
        assert "REQUIRED_BEAT_OMITTED" in body["available_script_warn_only_validation_codes"]
        # Both T11 validators' vocabularies are offered, once each.
        available = body["available_script_warn_only_validation_codes"]
        assert set(available) == set(PLOT_PLAN_VALIDATION_CODES) | set(
            RECAP_SCRIPT_VALIDATION_CODES
        )
        assert len(available) == len(set(available))


def test_script_warn_only_validation_codes_can_be_overridden_per_project(tmp_path: Path) -> None:
    with review_client_context(tmp_path) as (client, _, _):
        project_id = _create(client).json()["id"]
        updated = client.put(
            f"/api/v1/projects/{project_id}/generation-settings",
            json={
                "generation_quality": "balanced",
                "shot_pacing": "normal",
                "premium_fallback_allowed": False,
                "script_warn_only_validation_codes": ["UNKNOWN_BEAT"],
            },
            headers=OWNER,
        )
        assert updated.status_code == 200, updated.text
        body = updated.json()
        assert body["settings"]["script_warn_only_validation_codes"] == ["UNKNOWN_BEAT"]
        assert body["effective_script_warn_only_validation_codes"] == ["UNKNOWN_BEAT"]
        # The two vocabularies stay independent: the analysis codes are untouched.
        assert body["effective_warn_only_validation_codes"] == ["SCENE_SET_MISMATCH"]


def test_a_script_code_from_the_wrong_vocabulary_is_refused(tmp_path: Path) -> None:
    with review_client_context(tmp_path) as (client, _, _):
        response = _create(client, script_warn_only_validation_codes=["SCENE_SET_MISMATCH"])
        assert response.status_code == 422, response.text


def test_the_script_resolver_prefers_the_project_override() -> None:
    default = ["UNKNOWN_SOURCE_REFERENCE"]
    assert effective_script_warn_only_validation_codes(
        ProjectGenerationSettings(), default
    ) == frozenset({"UNKNOWN_SOURCE_REFERENCE"})
    assert effective_script_warn_only_validation_codes(
        ProjectGenerationSettings(script_warn_only_validation_codes=["DUPLICATE_ID"]), default
    ) == frozenset({"DUPLICATE_ID"})
    assert (
        effective_script_warn_only_validation_codes(
            ProjectGenerationSettings(script_warn_only_validation_codes=[]), default
        )
        == frozenset()
    )


def test_narration_quality_defaults_to_the_deployment_gate(tmp_path: Path) -> None:
    """No override stored: the response reports the deployment gate in effect."""
    with review_client_context(tmp_path) as (client, _, _):
        project_id = _create(client).json()["id"]
        body = client.get(
            f"/api/v1/projects/{project_id}/generation-settings", headers=OWNER
        ).json()
        assert body["settings"]["narration_warn_only_quality_codes"] is None
        assert body["settings"]["narration_quality_thresholds"] is None
        assert body["effective_narration_quality_thresholds"] == {
            "schema_version": "1.0",
            "min_wpm": 80,
            "max_wpm": 220,
            "min_alignment_coverage": 0.75,
            "max_clipping_ratio": 0.001,
            "max_leading_silence": 0.5,
            "max_trailing_silence": 0.7,
            "max_internal_silence": 1.5,
            "warn_only_codes": ["alignment_coverage"],
        }
        assert body["effective_narration_warn_only_quality_codes"] == ["alignment_coverage"]
        assert body["available_narration_warn_only_quality_codes"] == [
            "clipping",
            "leading_silence",
            "trailing_silence",
            "internal_silence",
            "speaking_rate",
            "alignment_coverage",
        ]


def test_narration_quality_can_be_overridden_per_project(tmp_path: Path) -> None:
    with review_client_context(tmp_path) as (client, factory, _):
        project_id = _create(client).json()["id"]
        updated = client.put(
            f"/api/v1/projects/{project_id}/generation-settings",
            json={
                "generation_quality": "balanced",
                "shot_pacing": "normal",
                "premium_fallback_allowed": False,
                "narration_warn_only_quality_codes": ["speaking_rate", "alignment_coverage"],
                "narration_quality_thresholds": {"min_alignment_coverage": 0.75},
            },
            headers=OWNER,
        )
        assert updated.status_code == 200, updated.text
        body = updated.json()
        assert body["settings"]["narration_warn_only_quality_codes"] == [
            "alignment_coverage",
            "speaking_rate",
        ]
        assert body["settings"]["narration_quality_thresholds"]["min_alignment_coverage"] == 0.75
        assert body["settings"]["narration_quality_thresholds"]["max_wpm"] is None
        effective = body["effective_narration_quality_thresholds"]
        assert effective["min_alignment_coverage"] == 0.75
        assert effective["max_wpm"] == 220
        assert effective["warn_only_codes"] == ["alignment_coverage", "speaking_rate"]
        assert body["effective_narration_warn_only_quality_codes"] == [
            "alignment_coverage",
            "speaking_rate",
        ]
        with factory() as session:
            project = session.get(Project, UUID(project_id))
            assert project is not None
            stored = project.settings["generation"]
            assert stored["narration_quality_thresholds"]["min_alignment_coverage"] == 0.75
        # An empty list is a real choice - every quality code fails - not "use the default".
        emptied = client.put(
            f"/api/v1/projects/{project_id}/generation-settings",
            json={
                "generation_quality": "balanced",
                "shot_pacing": "normal",
                "premium_fallback_allowed": False,
                "narration_warn_only_quality_codes": [],
            },
            headers=OWNER,
        )
        assert emptied.status_code == 200, emptied.text
        assert emptied.json()["effective_narration_warn_only_quality_codes"] == []
        assert emptied.json()["effective_narration_quality_thresholds"]["warn_only_codes"] == []


def test_narration_quality_overrides_are_accepted_at_creation(tmp_path: Path) -> None:
    with review_client_context(tmp_path) as (client, _, _):
        created = _create(
            client,
            narration_warn_only_quality_codes=["alignment_coverage", "leading_silence"],
            narration_quality_thresholds={"max_leading_silence": 1.0},
        )
        assert created.status_code == 201, created.text
        body = client.get(
            f"/api/v1/projects/{created.json()['id']}/generation-settings", headers=OWNER
        ).json()
        assert body["effective_narration_quality_thresholds"]["max_leading_silence"] == 1.0
        assert body["effective_narration_warn_only_quality_codes"] == [
            "alignment_coverage",
            "leading_silence",
        ]


def test_an_unknown_narration_quality_code_is_refused(tmp_path: Path) -> None:
    with review_client_context(tmp_path) as (client, _, _):
        response = _create(client, narration_warn_only_quality_codes=["NOT_A_CODE"])
        assert response.status_code == 422, response.text
        project_id = _create(client).json()["id"]
        updated = client.put(
            f"/api/v1/projects/{project_id}/generation-settings",
            json={
                "generation_quality": "balanced",
                "shot_pacing": "normal",
                "premium_fallback_allowed": False,
                "narration_warn_only_quality_codes": ["SCENE_SET_MISMATCH"],
            },
            headers=OWNER,
        )
        assert updated.status_code == 422, updated.text


def test_a_narration_gate_that_cannot_resolve_is_refused_before_it_is_stored(
    tmp_path: Path,
) -> None:
    """A project floor above the deployment ceiling is a 422, not a stored 500."""
    with review_client_context(tmp_path) as (client, _, _):
        project_id = _create(client).json()["id"]
        refused = client.put(
            f"/api/v1/projects/{project_id}/generation-settings",
            json={
                "generation_quality": "premium",
                "shot_pacing": "fast",
                "premium_fallback_allowed": True,
                "narration_quality_thresholds": {"min_wpm": 300},
            },
            headers=OWNER,
        )
        assert refused.status_code == 422, refused.text
        assert "min_wpm must be below max_wpm" in refused.text
        body = client.get(
            f"/api/v1/projects/{project_id}/generation-settings", headers=OWNER
        ).json()
        assert body["settings"]["generation_quality"] == "balanced"
        assert body["settings"]["narration_quality_thresholds"] is None
        assert _create(client, narration_quality_thresholds={"min_wpm": 300}).status_code == 422


def test_storyboard_warn_only_validation_codes_default_to_the_deployment_setting(
    tmp_path: Path,
) -> None:
    with review_client_context(tmp_path) as (client, _, _):
        project_id = _create(client).json()["id"]
        body = client.get(
            f"/api/v1/projects/{project_id}/generation-settings", headers=OWNER
        ).json()
        assert body["settings"]["storyboard_warn_only_validation_codes"] is None
        assert body["effective_storyboard_warn_only_validation_codes"] == [
            "continuity_contradiction"
        ]
        assert body["available_storyboard_warn_only_validation_codes"] == list(
            STORYBOARD_WARN_ONLY_ELIGIBLE_VALIDATION_CODES
        )
        # Every code the validator can emit may be tolerated.
        assert set(body["available_storyboard_warn_only_validation_codes"]) == set(
            get_args(StoryboardValidationCode)
        )


def test_storyboard_warn_only_validation_codes_can_be_overridden_per_project(
    tmp_path: Path,
) -> None:
    with review_client_context(tmp_path) as (client, _, _):
        project_id = _create(
            client, storyboard_warn_only_validation_codes=["missing_evidence_reference"]
        ).json()["id"]
        body = client.get(
            f"/api/v1/projects/{project_id}/generation-settings", headers=OWNER
        ).json()
        assert body["effective_storyboard_warn_only_validation_codes"] == [
            "missing_evidence_reference"
        ]
        updated = client.put(
            f"/api/v1/projects/{project_id}/generation-settings",
            json={
                "generation_quality": "balanced",
                "shot_pacing": "normal",
                "premium_fallback_allowed": False,
                "storyboard_warn_only_validation_codes": [],
            },
            headers=OWNER,
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["effective_storyboard_warn_only_validation_codes"] == []
        refused = client.put(
            f"/api/v1/projects/{project_id}/generation-settings",
            json={
                "generation_quality": "balanced",
                "shot_pacing": "normal",
                "premium_fallback_allowed": False,
                "storyboard_warn_only_validation_codes": ["SCENE_SET_MISMATCH"],
            },
            headers=OWNER,
        )
        assert refused.status_code == 422, refused.text
