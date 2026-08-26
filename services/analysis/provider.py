"""Provider-neutral Episode Analyst port."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from vidgen.contracts.episode_analysis import (
    EpisodeSynthesisRequest,
    ProviderEpisodeAnalysisResult,
    ProviderSceneAnalysisResult,
    SceneAnalysisRequest,
)


@dataclass(frozen=True, slots=True)
class GenerationContext:
    attempt_number: int = 1
    validation_errors_json: str | None = None


class EpisodeAnalysisProvider(Protocol):
    async def analyze_scene(
        self, request: SceneAnalysisRequest, context: GenerationContext
    ) -> ProviderSceneAnalysisResult: ...
    async def synthesize_episode(
        self, request: EpisodeSynthesisRequest, context: GenerationContext
    ) -> ProviderEpisodeAnalysisResult: ...
