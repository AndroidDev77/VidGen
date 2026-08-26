"""OpenAI Responses API adapter with strict JSON Schema structured output.

The model remains configuration, not a baked-in roadmap assumption. API shape was
verified against official Responses/Structured Outputs documentation on 2026-08-26,
the same verification pass T10's adapter documents.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import httpx

from services.script.provider import GenerationContext
from vidgen.contracts.script import (
    ComedyEditRequest,
    ComedyEditResult,
    ComedyWritingRequest,
    CompressedPlotPlan,
    PlotCompressionRequest,
    ProviderComedyEditResult,
    ProviderCompressedPlotResult,
    ProviderRecapScriptResult,
    RecapScript,
    ScriptProviderMetadata,
)

_PROMPT_VERSION = "comedy-script-v1"


@dataclass(frozen=True, slots=True)
class OpenAIScriptConfig:
    api_key: str
    compressor_model: str
    writer_model: str
    editor_model: str
    configuration_version: str = "openai-script-responses-v1"
    base_url: str = "https://api.openai.com/v1"
    timeout_seconds: float = 180


class OpenAIScriptGenerationProvider:
    def __init__(self, config: OpenAIScriptConfig, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self.client = client or httpx.AsyncClient(
            base_url=config.base_url, timeout=config.timeout_seconds
        )

    @property
    def configuration_version(self) -> str:
        return self.config.configuration_version

    async def _request(
        self,
        *,
        model: str,
        request: PlotCompressionRequest | ComedyWritingRequest | ComedyEditRequest,
        schema: type[CompressedPlotPlan] | type[RecapScript] | type[ComedyEditResult],
        prompt: str,
        context: GenerationContext,
    ) -> tuple[Any, dict[str, Any]]:
        user_content = request.model_dump_json()
        if context.validation_errors_json:
            user_content += "\nValidation errors to repair:\n" + context.validation_errors_json
        body = {
            "model": model,
            "input": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": user_content},
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": schema.__name__,
                    "strict": True,
                    "schema": _strict_schema(schema.model_json_schema()),
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
        parsed = schema.model_validate(json.loads(_response_text(payload)))
        return parsed, payload

    def _metadata(
        self,
        *,
        request: PlotCompressionRequest | ComedyWritingRequest | ComedyEditRequest,
        model: str,
        payload: dict[str, Any],
        operation: str,
        context: GenerationContext,
        rubric_version: str | None = None,
    ) -> ScriptProviderMetadata:
        usage = payload.get("usage", {})
        return ScriptProviderMetadata(
            provider="openai",
            model=model,
            provider_request_id=payload["id"],
            operation=operation,  # type: ignore[arg-type]
            attempt_number=context.attempt_number,
            input_hash=request.input_hash,
            prompt_version=request.prompt_version,
            contract_version=request.contract_version,
            rubric_version=rubric_version,
            redacted_response_metadata={"status": payload.get("status", "unknown")},
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
        )

    async def compress_plot(
        self, request: PlotCompressionRequest, context: GenerationContext
    ) -> ProviderCompressedPlotResult:
        output, payload = await self._request(
            model=self.config.compressor_model,
            request=request,
            schema=CompressedPlotPlan,
            prompt=_prompt("plot_compressor_v1.txt"),
            context=context,
        )
        metadata = self._metadata(
            request=request,
            model=self.config.compressor_model,
            payload=payload,
            operation="compress_plot",
            context=context,
        )
        return ProviderCompressedPlotResult(output=output, metadata=metadata)

    async def write_script(
        self, request: ComedyWritingRequest, context: GenerationContext
    ) -> ProviderRecapScriptResult:
        output, payload = await self._request(
            model=self.config.writer_model,
            request=request,
            schema=RecapScript,
            prompt=_prompt("comedy_writer_v1.txt"),
            context=context,
        )
        metadata = self._metadata(
            request=request,
            model=self.config.writer_model,
            payload=payload,
            operation="write_script",
            context=context,
        )
        return ProviderRecapScriptResult(output=output, metadata=metadata)

    async def edit_script(
        self, request: ComedyEditRequest, context: GenerationContext
    ) -> ProviderComedyEditResult:
        output, payload = await self._request(
            model=self.config.editor_model,
            request=request,
            schema=ComedyEditResult,
            prompt=_prompt("comedy_editor_v1.txt"),
            context=context,
        )
        metadata = self._metadata(
            request=request,
            model=self.config.editor_model,
            payload=payload,
            operation="edit_script",
            context=context,
            rubric_version=request.rubric_version,
        )
        return ProviderComedyEditResult(output=output, metadata=metadata)


def _prompt(filename: str) -> str:
    return (Path(__file__).parent / "prompts" / filename).read_text()


def _strict_schema(value: Any) -> Any:
    """Convert Pydantic JSON Schema objects to the strict Responses subset."""
    if isinstance(value, list):
        return [_strict_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: _strict_schema(item) for key, item in value.items()}
    if result.get("type") == "object" or "properties" in result:
        properties = result.get("properties", {})
        result["additionalProperties"] = False
        result["required"] = list(properties)
    return result


def _response_text(payload: dict[str, Any]) -> str:
    if payload.get("status") == "incomplete":
        raise ValueError("OpenAI response was incomplete")
    for output in payload.get("output", []):
        for content in output.get("content", []):
            if content.get("type") == "refusal":
                raise ValueError("OpenAI refused the script generation request")
            if content.get("type") == "output_text" and isinstance(content.get("text"), str):
                return cast(str, content["text"])
    raise ValueError("OpenAI response contained no output text")
