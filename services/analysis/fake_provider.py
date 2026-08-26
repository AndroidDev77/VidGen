"""Deterministic, zero-cost provider used by tests and local verification."""

from __future__ import annotations

from uuid import UUID, uuid5

from services.analysis.provider import GenerationContext
from vidgen.contracts.episode_analysis import (
    CanonicalScene,
    EpisodeAnalysis,
    EpisodeSynthesisRequest,
    ProviderEpisodeAnalysisResult,
    ProviderMetadata,
    ProviderSceneAnalysisResult,
    SceneAnalysisRequest,
    SceneAnalysisResult,
)

FAKE_NAMESPACE = UUID("89fb52d2-b9ad-4825-af84-919d13c80ccc")


class FakeEpisodeAnalysisProvider:
    """Returns evidence-only output and records logical submissions for assertions."""

    provider = "fake"
    model = "deterministic-episode-v1"

    def __init__(self) -> None:
        self.scene_results: dict[UUID, SceneAnalysisResult] = {}
        self.submissions: list[str] = []

    def _metadata(
        self,
        request: SceneAnalysisRequest | EpisodeSynthesisRequest,
        context: GenerationContext,
    ) -> ProviderMetadata:
        self.submissions.append(request.idempotency_key)
        return ProviderMetadata(
            provider=self.provider,
            model=self.model,
            provider_request_id=str(uuid5(FAKE_NAMESPACE, request.idempotency_key)),
            attempt_number=context.attempt_number,
            prompt_version=request.prompt_version,
            contract_version=request.contract_version,
            input_hash=request.input_hash,
        )

    async def analyze_scene(
        self, request: SceneAnalysisRequest, context: GenerationContext
    ) -> ProviderSceneAnalysisResult:
        reference = next(
            item for item in request.evidence_references if item.scene_id == request.scene_id
        )
        output = SceneAnalysisResult(
            scene_id=request.scene_id,
            sequence=request.sequence,
            source_start_ms=request.source_start_ms,
            source_end_ms=request.source_end_ms,
            summary=f"Evidence scene {request.sequence}",
            dramatic_purpose="Source chronology",
            confidence=1,
            source_references=[reference],
        )
        self.scene_results[request.scene_id] = output
        return ProviderSceneAnalysisResult(output=output, metadata=self._metadata(request, context))

    async def synthesize_episode(
        self, request: EpisodeSynthesisRequest, context: GenerationContext
    ) -> ProviderEpisodeAnalysisResult:
        scenes = [self.scene_results[item] for item in request.scene_result_ids]
        output = EpisodeAnalysis(
            episode_id=uuid5(FAKE_NAMESPACE, f"episode:{request.input_hash}"),
            project_id=request.project_id,
            source_video_id=request.source_video_id,
            evidence_package_id=request.evidence_package_id,
            duration_ms=request.duration_ms,
            scenes=[
                CanonicalScene(
                    scene_id=item.scene_id,
                    sequence=item.sequence,
                    source_start_ms=item.source_start_ms,
                    source_end_ms=item.source_end_ms,
                    summary=item.summary,
                    dramatic_purpose=item.dramatic_purpose,
                    confidence=item.confidence,
                    source_references=item.source_references,
                )
                for item in scenes
            ],
            source_references=[ref for item in scenes for ref in item.source_references],
        )
        return ProviderEpisodeAnalysisResult(
            output=output, metadata=self._metadata(request, context)
        )
