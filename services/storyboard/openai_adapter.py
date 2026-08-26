"""OpenAI Responses adapter for the Storyboard Director.

No OpenAI SDK response object crosses this boundary; the adapter parses the
response into the provider-neutral T13 contracts. The model stays configuration
rather than a baked-in assumption.

API shape verified on 2026-08-26 against the official OpenAI structured-output
documentation: on the Responses API the JSON Schema is supplied under
``text.format`` with sibling ``type``/``name``/``schema``/``strict`` fields;
strict mode requires ``additionalProperties: false`` and an exhaustive
``required`` list on every object, forbids ``$ref``, and caps nesting depth, so
``$defs`` are inlined before the schema is sent.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, cast

import httpx

from services.storyboard.providers import (
    DEFAULT_STORYBOARD_MODEL,
    OPENAI_BASE_URL,
    OPENAI_RESPONSES_PATH,
    PROMPT_VERSION,
)
from vidgen.contracts.episode_analysis import StructuredNote
from vidgen.contracts.storyboard import (
    ContinuityState,
    StoryboardProviderRequest,
    StoryboardProviderResult,
    StoryboardShotProposal,
)

SYSTEM_PROMPT = (
    "You are the Storyboard Director for an animated comedy recap. You convert one measured "
    "narration segment into ordered visual shot proposals.\n"
    "Rules:\n"
    "1. Show the action; never restate the narration text on screen.\n"
    "2. Cover every narration word exactly once. The word ranges of your proposals must tile "
    "the segment from word 0 to the final word with no gap and no overlap.\n"
    "3. Split only at the approved clause, sentence, or beat boundaries you are given.\n"
    "4. desired_duration_us is a preference only. A deterministic retimer owns final timing and "
    "will snap your boundaries to the measured narration.\n"
    "5. Never exceed the capability profile's max_characters_per_shot or max_reference_images, "
    "and use only its supported camera movements and transitions.\n"
    "6. Reference only the characters and locations you are given. If the segment has an "
    "anonymous speaker, reference no character identity at all.\n"
    "7. Declare each shot's incoming assumptions and expected outgoing continuity state. Any "
    "deliberate continuity change must carry an explaining warning.\n"
    "8. Do not write provider prompts; a later stage compiles these structured shots."
)


@dataclass(frozen=True, slots=True)
class OpenAIStoryboardConfig:
    api_key: str
    model: str = DEFAULT_STORYBOARD_MODEL
    base_url: str = OPENAI_BASE_URL
    timeout_seconds: float = 180
    configuration_version: str = "openai-storyboard-responses-v1"


class _DirectorOutput(StoryboardProviderResult):
    """Only the creative half of the result is asked of the model."""


class OpenAIStoryboardDirector:
    name = "openai"

    def __init__(
        self, config: OpenAIStoryboardConfig, client: httpx.AsyncClient | None = None
    ) -> None:
        if not config.api_key:
            raise ValueError("OpenAI API key is required")
        self.config = config
        self.model = config.model
        self.client = client or httpx.AsyncClient(
            base_url=config.base_url, timeout=config.timeout_seconds
        )

    async def propose(self, request: StoryboardProviderRequest) -> StoryboardProviderResult:
        body = {
            "model": self.config.model,
            "input": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _user_content(request)},
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "StoryboardDirectorOutput",
                    "strict": True,
                    "schema": strict_schema(_output_schema()),
                }
            },
        }
        response = await self.client.post(
            OPENAI_RESPONSES_PATH,
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Idempotency-Key": request.idempotency_key,
            },
            json=body,
        )
        response.raise_for_status()
        payload = cast(dict[str, Any], response.json())
        parsed = json.loads(response_text(payload))
        usage = payload.get("usage") or {}
        return StoryboardProviderResult(
            proposals=[
                StoryboardShotProposal.model_validate(item) for item in parsed.get("proposals", [])
            ],
            expected_incoming_continuity=ContinuityState.model_validate(
                parsed["expected_incoming_continuity"]
            ),
            expected_outgoing_continuity=ContinuityState.model_validate(
                parsed["expected_outgoing_continuity"]
            ),
            provider=self.name,
            model=self.config.model,
            provider_request_id=str(payload["id"]),
            idempotency_key=request.idempotency_key,
            attempt_number=request.attempt_number,
            usage={
                key: value
                for key, value in usage.items()
                if isinstance(value, int | float) and not isinstance(value, bool)
            },
            # Only a bounded status field is retained; prompts, credentials, and raw
            # payloads never leave this adapter.
            redacted_response_metadata={
                "status": str(payload.get("status", "unknown")),
                "prompt_version": PROMPT_VERSION,
                "configuration_version": self.config.configuration_version,
            },
            warnings=[StructuredNote.model_validate(item) for item in parsed.get("warnings", [])],
        )

    async def aclose(self) -> None:
        await self.client.aclose()


def _user_content(request: StoryboardProviderRequest) -> str:
    """The request envelope minus anything the model must not be handed."""
    payload = request.model_dump(mode="json")
    payload.pop("trace_context", None)
    payload.pop("provider_options", None)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _output_schema() -> dict[str, Any]:
    schema = _DirectorOutput.model_json_schema()
    properties = cast(dict[str, Any], schema.get("properties", {}))
    for provider_field in (
        "provider",
        "model",
        "provider_request_id",
        "idempotency_key",
        "attempt_number",
        "usage",
        "redacted_response_metadata",
        "schema_version",
    ):
        properties.pop(provider_field, None)
    schema["properties"] = properties
    return schema


def strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Inline ``$defs`` and apply the strict structured-output subset.

    Strict mode rejects ``$ref``, so definitions are inlined; every object gets
    ``additionalProperties: false`` and an exhaustive ``required`` list.
    """
    definitions = cast(dict[str, Any], schema.get("$defs", {}))
    inlined = _inline(schema, definitions, depth=0)
    if isinstance(inlined, dict):
        inlined.pop("$defs", None)
    return cast(dict[str, Any], _strict(inlined))


def _inline(value: Any, definitions: dict[str, Any], *, depth: int) -> Any:
    if depth > 64:
        raise ValueError("storyboard provider schema is too deeply nested to inline")
    if isinstance(value, list):
        return [_inline(item, definitions, depth=depth + 1) for item in value]
    if not isinstance(value, dict):
        return value
    reference = value.get("$ref")
    if isinstance(reference, str) and reference.startswith("#/$defs/"):
        target = definitions.get(reference.split("/")[-1])
        if target is None:
            raise ValueError(f"unresolved schema reference {reference}")
        merged = {key: item for key, item in value.items() if key != "$ref"}
        return _inline({**target, **merged}, definitions, depth=depth + 1)
    return {key: _inline(item, definitions, depth=depth + 1) for key, item in value.items()}


def _strict(value: Any) -> Any:
    if isinstance(value, list):
        return [_strict(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: _strict(item) for key, item in value.items()}
    if result.get("type") == "object" or "properties" in result:
        properties = cast(dict[str, Any], result.get("properties", {}))
        result["additionalProperties"] = False
        result["required"] = list(properties)
    return result


def response_text(payload: dict[str, Any]) -> str:
    if payload.get("status") == "incomplete":
        raise ValueError("OpenAI storyboard response was incomplete")
    for output in payload.get("output", []):
        for content in output.get("content", []):
            if content.get("type") == "refusal":
                raise ValueError("OpenAI refused the storyboard direction request")
            if content.get("type") == "output_text" and isinstance(content.get("text"), str):
                return cast(str, content["text"])
    raise ValueError("OpenAI storyboard response contained no output text")
