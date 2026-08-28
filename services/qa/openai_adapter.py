"""Configured OpenAI visual-agent adapter.

The adapter is the only place that knows about an OpenAI request or response
shape. It converts one bounded :class:`VisualQAProviderRequest` plus its frame
bytes into a strict structured-output request, and converts the reply back into
a :class:`VisualQAProviderResult`. Nothing else leaks: no SDK object, no signed
URL, no raw provider payload, and no unrestricted model reasoning.

Frames and references are inlined as ``data:`` URLs for the duration of the call
only. They are never persisted, logged, or written into a contract.

API shape follows the same Responses/Structured Outputs usage the other verified
adapters in this repository use. The model IDs come from the central registry in
``services.qa.visual_agent``; changing a production model requires checking the
provider's current official documentation first.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import httpx

from services.qa.visual_agent import (
    DEFAULT_REGISTRY,
    VisualAgentCall,
    VisualAgentError,
    VisualQARole,
)
from vidgen.contracts.visual_qa import VisualQAProviderResult

PROMPTS = {
    VisualQARole.LUNA_FIRST_PASS: "visual_qa_v1.txt",
    VisualQARole.TERRA_ADJUDICATOR: "visual_qa_adjudication_v1.txt",
}


@dataclass(frozen=True, slots=True)
class OpenAIVisualQAConfig:
    api_key: str
    model: str
    configuration_version: str = "openai-responses-v1"
    base_url: str = "https://api.openai.com/v1"
    timeout_seconds: float = 180


class OpenAIVisualAgent:
    """A configured production visual agent for one role."""

    def __init__(
        self,
        *,
        api_key: str,
        role: VisualQARole = VisualQARole.LUNA_FIRST_PASS,
        model: str | None = None,
        client: httpx.AsyncClient | None = None,
        base_url: str = "https://api.openai.com/v1",
    ) -> None:
        registered = DEFAULT_REGISTRY[role]
        self.config = OpenAIVisualQAConfig(
            api_key=api_key, model=model or registered.model, base_url=base_url
        )
        self.role = role
        self._client = client
        self._base_url = base_url

    @property
    def name(self) -> str:
        return "openai"

    @property
    def model(self) -> str:
        return self.config.model

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url, timeout=self.config.timeout_seconds
            )
        return self._client

    async def evaluate(self, call: VisualAgentCall) -> VisualQAProviderResult:
        response = await self.client.post(
            "/responses",
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                # The QA attempt identity is the application's own idempotency
                # key, so a retried activity never buys a second evaluation.
                "Idempotency-Key": call.request.qa_attempt_identity,
            },
            json=self._body(call),
        )
        response.raise_for_status()
        payload = response.json()
        parsed = _parse(payload)
        # The provider never supplies the canonical identity or the total score;
        # both are re-bound here from the request the application issued.
        return parsed.model_copy(
            update={
                "qa_attempt_identity": call.request.qa_attempt_identity,
                "attempt_type": call.request.attempt_type,
                "provider": self.name,
                "model": self.config.model,
                "provider_request_id": str(payload.get("id", ""))[:255] or None,
                "usage": {
                    key: float(value)
                    for key, value in (payload.get("usage") or {}).items()
                    if isinstance(value, (int, float))
                },
                "redacted_metadata": {
                    "status": str(payload.get("status", "unknown")),
                    "role": self.role.value,
                    "configuration_version": self.config.configuration_version,
                },
            }
        )

    def _body(self, call: VisualAgentCall) -> dict[str, Any]:
        request = call.request
        content: list[dict[str, Any]] = [
            {
                "type": "input_text",
                "text": json.dumps(
                    request.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
                ),
            }
        ]
        for frame in call.frames:
            content.append(
                {
                    "type": "input_text",
                    "text": (
                        f"sample_id={frame.sample_id} sequence={frame.sequence} "
                        f"shot_relative_timestamp_us={frame.shot_relative_timestamp_us}"
                    ),
                }
            )
            content.append(
                {
                    "type": "input_image",
                    "image_url": _data_url(frame.content, frame.media_type),
                }
            )
        for reference in call.references:
            content.append(
                {
                    "type": "input_text",
                    "text": (
                        f"approved_reference asset_id={reference.asset_id} role={reference.role}"
                    ),
                }
            )
            content.append(
                {
                    "type": "input_image",
                    "image_url": _data_url(reference.content, reference.media_type),
                }
            )
        if call.first_pass is not None:
            content.append(
                {
                    "type": "input_text",
                    "text": "first_pass_result="
                    + json.dumps(
                        call.first_pass.model_dump(
                            mode="json",
                            exclude={"redacted_metadata", "usage", "provider_request_id"},
                        ),
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                }
            )
        return {
            "model": self.config.model,
            "input": [
                {"role": "system", "content": _prompt(self.role)},
                {"role": "user", "content": content},
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "VisualQAProviderResult",
                    "strict": True,
                    "schema": _strict_schema(VisualQAProviderResult.model_json_schema()),
                }
            },
        }


def _prompt(role: VisualQARole) -> str:
    return (Path(__file__).parent / "prompts" / PROMPTS[role]).read_text()


def _data_url(content: bytes, media_type: str) -> str:
    return f"data:{media_type};base64,{base64.b64encode(content).decode()}"


def _strict_schema(value: Any) -> Any:
    """Convert Pydantic JSON Schema objects to the strict Responses subset."""
    if isinstance(value, list):
        return [_strict_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: _strict_schema(item) for key, item in value.items()}
    if result.get("type") == "object" or "properties" in result:
        result["additionalProperties"] = False
        result["required"] = list(result.get("properties", {}))
    return result


def _parse(payload: dict[str, Any]) -> VisualQAProviderResult:
    if payload.get("status") == "incomplete":
        raise VisualAgentError("the visual agent returned an incomplete response")
    for output in payload.get("output", []):
        for item in output.get("content", []):
            if item.get("type") == "refusal":
                raise VisualAgentError("the visual agent refused the evaluation request")
            if item.get("type") == "output_text" and isinstance(item.get("text"), str):
                try:
                    return VisualQAProviderResult.model_validate_json(cast(str, item["text"]))
                except ValueError as error:
                    raise VisualAgentError(
                        "the visual agent returned a result that failed contract validation"
                    ) from error
    raise VisualAgentError("the visual agent response contained no structured output")
