"""Per-project visual-QA warn-only codes: resolution and the worker's wiring."""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.api.settings import APISettings
from services.generation.settings import (
    effective_visual_qa_thresholds,
    effective_visual_qa_warn_only_codes,
    with_generation_settings,
)
from services.qa.rubric import THRESHOLDS
from tests.storyboard_fixtures import build_fixture
from vidgen.contracts.generation import ProjectGenerationSettings
from vidgen.contracts.visual_qa import DEFAULT_VISUAL_QA_WARN_ONLY_CODES
from workers.temporal_worker import production_handlers

DEPLOYMENT = THRESHOLDS.model_copy(update={"warn_only_codes": ["PROMPT_TOO_COMPLEX"]})


def test_without_an_override_the_deployment_policy_applies_unchanged() -> None:
    assert effective_visual_qa_thresholds(ProjectGenerationSettings(), DEPLOYMENT) == DEPLOYMENT


def test_a_project_replaces_the_warn_only_set_outright() -> None:
    codes = ["WRONG_CHARACTER_IDENTITY", "ANATOMY_BREAKAGE"]
    generation = ProjectGenerationSettings(visual_qa_warn_only_codes=codes)
    resolved = effective_visual_qa_thresholds(generation, DEPLOYMENT)
    assert resolved.warn_only_codes == ["ANATOMY_BREAKAGE", "WRONG_CHARACTER_IDENTITY"]
    # Every other threshold is still the deployment's versioned policy.
    assert resolved.model_dump(exclude={"warn_only_codes"}) == DEPLOYMENT.model_dump(
        exclude={"warn_only_codes"}
    )
    assert effective_visual_qa_warn_only_codes(generation, ["PROMPT_TOO_COMPLEX"]) == (
        frozenset(codes)
    )
    emptied = ProjectGenerationSettings(visual_qa_warn_only_codes=[])
    assert effective_visual_qa_thresholds(emptied, DEPLOYMENT).warn_only_codes == []


def test_the_generation_settings_refuse_an_unknown_repair_code() -> None:
    with pytest.raises(ValueError, match="unknown visual QA repair codes: alignment_coverage"):
        ProjectGenerationSettings(visual_qa_warn_only_codes=["alignment_coverage"])


def test_stored_settings_without_the_new_field_still_resolve() -> None:
    stored = ProjectGenerationSettings().model_dump(mode="json")
    del stored["visual_qa_warn_only_codes"]
    assert ProjectGenerationSettings.model_validate(stored).visual_qa_warn_only_codes is None


def test_the_worker_hands_the_gate_the_projects_policy(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path, database_name="visual-qa-warn-only.db")
    settings = APISettings(_env_file=None, temporal_allow_fake_providers=True)
    # No override: the deployment default applies.
    resolved = production_handlers._visual_qa_thresholds(
        fixture.session, settings, fixture.project.id
    )
    assert resolved.warn_only_codes == sorted(DEFAULT_VISUAL_QA_WARN_ONLY_CODES)
    fixture.project.settings = with_generation_settings(
        fixture.project.settings,
        ProjectGenerationSettings(visual_qa_warn_only_codes=["MISSING_REQUIRED_PROP"]),
    )
    fixture.session.commit()
    resolved = production_handlers._visual_qa_thresholds(
        fixture.session, settings, fixture.project.id
    )
    assert resolved.warn_only_codes == ["MISSING_REQUIRED_PROP"]
    assert resolved.threshold_version == THRESHOLDS.threshold_version
