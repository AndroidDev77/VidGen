"""``VIDGEN_ANALYSIS_CONCURRENCY``: the setting, and the worker's wiring of it.

The T10 map fan-out used to be hard-coded. The ceiling is not the machine's,
it is the provider account's concurrent-request allowance - the most
restrictive OpenAI tiers allow two, and every request above that is rejected
with 429 the moment it is sent - so the fan-out is configuration.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from apps.api.settings import APISettings
from services.analysis.fake_provider import FakeEpisodeAnalysisProvider
from services.analysis.pipeline import EpisodeAnalysisPipeline
from services.analysis.provider import GenerationContext
from tests.test_episode_analysis_pipeline import _database
from vidgen.contracts.episode_analysis import (
    ProviderSceneAnalysisResult,
    SceneAnalysisRequest,
)
from vidgen.contracts.workflow import StageActivityInput
from workers.temporal_worker import production_handlers


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    # A developer's own environment must not decide the outcome of these tests.
    monkeypatch.delenv("VIDGEN_ANALYSIS_CONCURRENCY", raising=False)


class _ConcurrencyRecordingProvider(FakeEpisodeAnalysisProvider):
    """Counts how many scene calls are in flight at the same moment."""

    def __init__(self) -> None:
        super().__init__()
        self.in_flight = 0
        self.peak_in_flight = 0

    async def analyze_scene(
        self, request: SceneAnalysisRequest, context: GenerationContext
    ) -> ProviderSceneAnalysisResult:
        self.in_flight += 1
        self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        try:
            # Yield to the loop so a second permitted call can start before
            # this one settles; without it the peak is always one.
            await asyncio.sleep(0)
            return await super().analyze_scene(request, context)
        finally:
            self.in_flight -= 1


def test_the_default_is_two_and_the_environment_overrides_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert APISettings(_env_file=None).analysis_concurrency == 2
    monkeypatch.setenv("VIDGEN_ANALYSIS_CONCURRENCY", "6")
    assert APISettings(_env_file=None).analysis_concurrency == 6


@pytest.mark.parametrize("value", ["0", "-1"])
def test_a_fan_out_below_one_is_refused(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    # Zero would deadlock the map phase rather than slow it down.
    monkeypatch.setenv("VIDGEN_ANALYSIS_CONCURRENCY", value)
    with pytest.raises(ValidationError):
        APISettings(_env_file=None)


@pytest.mark.asyncio
async def test_the_pipeline_never_exceeds_its_configured_fan_out(tmp_path: Path) -> None:
    session, blobs, project, evidence = _database(tmp_path)
    serial = _ConcurrencyRecordingProvider()
    await EpisodeAnalysisPipeline(session, blobs, serial, concurrency=1).process(
        project_id=project.id, evidence_package_id=evidence.id, idempotency_key="serial"
    )
    assert serial.peak_in_flight == 1

    paired = _ConcurrencyRecordingProvider()
    second = tmp_path / "paired"
    second.mkdir()
    session, blobs, project, evidence = _database(second)
    await EpisodeAnalysisPipeline(session, blobs, paired, concurrency=2).process(
        project_id=project.id, evidence_package_id=evidence.id, idempotency_key="paired"
    )
    assert paired.peak_in_flight == 2


def test_the_worker_hands_the_pipeline_the_configured_fan_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session, blobs, project, evidence = _database(tmp_path)
    recorded: dict[str, Any] = {}

    class _RecordingPipeline(EpisodeAnalysisPipeline):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            recorded.update(kwargs)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(production_handlers, "EpisodeAnalysisPipeline", _RecordingPipeline)
    settings = APISettings(
        _env_file=None, temporal_allow_fake_providers=True, analysis_concurrency=3
    )
    request = StageActivityInput(
        project_id=project.id,
        source_video_id=evidence.source_video_id,
        stage="analyze_episode",
        idempotency_key="analysis-concurrency",
    )
    production_handlers._analyze_episode(session, blobs, settings, request)
    assert recorded["concurrency"] == 3
