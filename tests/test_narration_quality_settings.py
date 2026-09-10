"""Per-project narration quality gates: resolution and the worker's wiring."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from apps.api.settings import APISettings
from services.generation.settings import (
    GenerationSettingsError,
    effective_narration_quality_thresholds,
    effective_narration_warn_only_quality_codes,
    with_generation_settings,
)
from tests.storyboard_fixtures import build_fixture
from vidgen.contracts.generation import ProjectGenerationSettings
from vidgen.contracts.narration import (
    NarrationQualityThresholdOverrides,
    NarrationQualityThresholds,
)
from vidgen.contracts.workflow import StageActivityInput
from workers.temporal_worker import production_handlers

DEPLOYMENT = NarrationQualityThresholds(max_wpm=200, warn_only_codes=["alignment_coverage"])


def test_without_an_override_the_deployment_gate_applies_unchanged() -> None:
    assert effective_narration_quality_thresholds(ProjectGenerationSettings(), DEPLOYMENT) == (
        DEPLOYMENT
    )


def test_a_project_overrides_only_the_limits_it_sets() -> None:
    generation = ProjectGenerationSettings(
        narration_quality_thresholds=NarrationQualityThresholdOverrides(
            min_alignment_coverage=0.75, max_leading_silence=1.0
        )
    )
    resolved = effective_narration_quality_thresholds(generation, DEPLOYMENT)
    assert resolved == NarrationQualityThresholds(
        max_wpm=200,
        min_alignment_coverage=0.75,
        max_leading_silence=1.0,
        warn_only_codes=["alignment_coverage"],
    )


def test_a_project_replaces_the_warn_only_set_outright() -> None:
    codes = ["speaking_rate", "clipping"]
    generation = ProjectGenerationSettings(narration_warn_only_quality_codes=codes)
    resolved = effective_narration_quality_thresholds(generation, DEPLOYMENT)
    assert resolved.warn_only_codes == ["clipping", "speaking_rate"]
    assert effective_narration_warn_only_quality_codes(generation, ["alignment_coverage"]) == (
        frozenset(codes)
    )
    # An empty list is a real choice - every code fails - not "use the default".
    strict = ProjectGenerationSettings(narration_warn_only_quality_codes=[])
    assert effective_narration_quality_thresholds(strict, DEPLOYMENT).warn_only_codes == []
    assert effective_narration_warn_only_quality_codes(strict, ["alignment_coverage"]) == (
        frozenset()
    )


def test_an_override_that_cannot_combine_with_the_deployment_is_refused() -> None:
    """min_wpm alone is valid, but not above the deployment's max_wpm."""
    generation = ProjectGenerationSettings(
        narration_quality_thresholds=NarrationQualityThresholdOverrides(min_wpm=250)
    )
    with pytest.raises(GenerationSettingsError, match="min_wpm must be below max_wpm"):
        effective_narration_quality_thresholds(generation, DEPLOYMENT)


def test_the_generation_settings_refuse_an_unknown_quality_code() -> None:
    with pytest.raises(ValueError, match="unknown narration quality codes: NOT_A_CODE"):
        ProjectGenerationSettings(narration_warn_only_quality_codes=["NOT_A_CODE"])
    with pytest.raises(ValueError, match="min_wpm must be below max_wpm"):
        NarrationQualityThresholdOverrides(min_wpm=200, max_wpm=100)


def test_stored_settings_without_the_new_fields_still_resolve() -> None:
    """A project persisted before these settings existed has no override."""
    stored = ProjectGenerationSettings().model_dump(mode="json")
    del stored["narration_warn_only_quality_codes"]
    del stored["narration_quality_thresholds"]
    generation = ProjectGenerationSettings.model_validate(stored)
    assert generation.narration_warn_only_quality_codes is None
    assert generation.narration_quality_thresholds is None
    assert effective_narration_quality_thresholds(generation, DEPLOYMENT) == DEPLOYMENT


def test_the_narration_handler_hands_the_pipeline_the_projects_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = build_fixture(tmp_path, database_name="handler.db")
    generation = ProjectGenerationSettings(
        narration_warn_only_quality_codes=[],
        narration_quality_thresholds=NarrationQualityThresholdOverrides(min_alignment_coverage=0.7),
    )
    fixture.project.settings = {
        **with_generation_settings(fixture.project.settings, generation),
        "voice_profile_id": str(fixture.narration_run.voice_profile_id),
    }
    fixture.session.commit()
    captured: dict[str, Any] = {}

    class RecordingPipeline:
        def __init__(self, session: object, blob_store: object, provider: object, **kwargs: Any):
            captured["provider"] = provider
            captured.update(kwargs)

        async def process(self, **kwargs: Any) -> SimpleNamespace:
            captured["process"] = kwargs
            return SimpleNamespace(narration_run_id=uuid4(), preview_manifest_asset_id=None)

    monkeypatch.setattr(production_handlers, "NarrationPipeline", RecordingPipeline)
    settings = APISettings(
        _env_file=None,
        temporal_allow_fake_providers=True,
        narration_max_wpm=200,
        narration_warn_only_quality_codes=["speaking_rate"],
    )
    request = StageActivityInput(
        project_id=fixture.project.id,
        source_video_id=uuid4(),
        stage="narration",
        idempotency_key="narration:handler",
    )
    result = production_handlers._generate_narration(
        fixture.session, fixture.blobs, settings, request
    )
    assert result.stage == "narration"
    assert captured["provider"].name == "fake"
    assert captured["thresholds"] == NarrationQualityThresholds(
        max_wpm=200, min_alignment_coverage=0.7, warn_only_codes=[]
    )
    assert captured["process"] == {
        "project_id": fixture.project.id,
        "voice_profile_id": fixture.narration_run.voice_profile_id,
        "idempotency_key": "narration:handler",
    }
