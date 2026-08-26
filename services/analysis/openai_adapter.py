"""OpenAI Responses API adapter with strict JSON Schema structured output.

The model remains configuration, not a baked-in roadmap assumption. API shape was
verified against official Responses/Structured Outputs documentation on 2026-08-26.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

from services.analysis.provider import GenerationContext
from vidgen.contracts.episode_analysis import (
    EpisodeAnalysis,
    EpisodeSynthesisRequest,
    ProviderEpisodeAnalysisResult,
    ProviderMetadata,
    ProviderSceneAnalysisResult,
    SceneAnalysisRequest,
    SceneAnalysisResult,
)


@dataclass(frozen=True, slots=True)
class OpenAIAnalysisConfig:
    api_key: str
    model: str
    configuration_version: str = "openai-responses-v1"
    base_url: str = "https://api.openai.com/v1"
    timeout_seconds: float = 120


class OpenAIEpisodeAnalysisProvider:
    def __init__(
        self, config: OpenAIAnalysisConfig, client: httpx.AsyncClient | None = None
    ) -> None:
        self.config = config
        self.client = client or httpx.AsyncClient(
            base_url=config.base_url, timeout=config.timeout_seconds
        )

    async def _request(
        self,
        *,
        request: SceneAnalysisRequest | EpisodeSynthesisRequest,
        schema: type[SceneAnalysisResult] | type[EpisodeAnalysis],
        prompt: str,
        context: GenerationContext,
    ) -> tuple[Any, ProviderMetadata]:
        body = {
            "model": self.config.model,
            "input": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": request.model_dump_json()},
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": schema.__name__,
                    "strict": True,
                    "schema": schema.model_json_schema(),
                }
            },
        }
        response = await self.client.post(
            "/responses",
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Idempotency-Key": request.idempotency_key,
            },
            json=body,
        )
        response.raise_for_status()
        payload = response.json()
        parsed = schema.model_validate(json.loads(payload["output_text"]))
        usage = payload.get("usage", {})
        metadata = ProviderMetadata(
            provider="openai",
            model=self.config.model,
            provider_request_id=payload["id"],
            attempt_number=context.attempt_number,
            prompt_version=request.prompt_version,
            contract_version=request.contract_version,
            input_hash=request.input_hash,
            redacted_response_metadata={"status": payload.get("status", "unknown")},
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
        )
        return parsed, metadata

    async def analyze_scene(
        self, request: SceneAnalysisRequest, context: GenerationContext
    ) -> ProviderSceneAnalysisResult:
        output, metadata = await self._request(
            request=request,
            schema=SceneAnalysisResult,
            prompt="Analyze only supported scene evidence. Preserve uncertainty.",
            context=context,
        )
        return ProviderSceneAnalysisResult(output=output, metadata=metadata)

    async def synthesize_episode(
        self, request: EpisodeSynthesisRequest, context: GenerationContext
    ) -> ProviderEpisodeAnalysisResult:
        output, metadata = await self._request(
            request=request,
            schema=EpisodeAnalysis,
            prompt="Reduce validated scene analyses without adding facts.",
            context=context,
        )
        return ProviderEpisodeAnalysisResult(output=output, metadata=metadata)
