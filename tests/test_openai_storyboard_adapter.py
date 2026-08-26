"""Mocked production-adapter contract tests. No real provider call is ever made."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import httpx
import pytest

from services.storyboard.fake_provider import FakeStoryboardDirector
from services.storyboard.openai_adapter import (
    OpenAIStoryboardConfig,
    OpenAIStoryboardDirector,
    response_text,
    strict_schema,
)
from services.storyboard.providers import CONTINUOUS_PROFILE
from vidgen.contracts.storyboard import (
    CONTRACT_VERSION,
    ContinuityState,
    NarrationBoundary,
    StoryboardProviderRequest,
)


def _request(attempt: int = 1) -> StoryboardProviderRequest:
    return StoryboardProviderRequest(
        idempotency_key="storyboard-key:1",
        project_id=uuid4(),
        episode_model_id=uuid4(),
        episode_model_hash="a" * 64,
        script_id=uuid4(),
        script_version=1,
        script_segment_id=uuid4(),
        segment_sequence=0,
        narration_run_id=uuid4(),
        narration_segment_id=uuid4(),
        narration_asset_id=uuid4(),
        measured_duration_us=4_000_000,
        narration_text="The toaster is on fire again.",
        word_timings=[
            NarrationBoundary(word_index=index, offset_us=(index + 1) * 500_000, kind="word")
            for index in range(6)
        ],
        incoming_continuity=ContinuityState(),
        capability=CONTINUOUS_PROFILE,
        contract_version=CONTRACT_VERSION,
        prompt_version="storyboard-director-v1",
        trace_context={"traceparent": "00-" + "0" * 32 + "-" + "0" * 16 + "-01"},
        attempt_number=attempt,
    )


async def _director_payload(request: StoryboardProviderRequest) -> dict[str, Any]:
    """A response body shaped exactly like the strict schema the adapter sends."""
    result = await FakeStoryboardDirector().propose(request)
    payload = result.model_dump(mode="json")
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
        payload.pop(provider_field, None)
    return payload


def _client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.openai.test/v1"
    )


@pytest.mark.asyncio
async def test_adapter_sends_strict_structured_output_and_parses_the_response() -> None:
    request = _request()
    body = await _director_payload(request)
    captured: dict[str, Any] = {}

    async def handler(http_request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(http_request.content)
        captured["key"] = http_request.headers["Idempotency-Key"]
        captured["auth"] = http_request.headers["Authorization"]
        return httpx.Response(
            200,
            json={
                "id": "resp_storyboard_1",
                "status": "completed",
                "usage": {"input_tokens": 1200, "output_tokens": 900},
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": json.dumps(body)}],
                    }
                ],
            },
        )

    director = OpenAIStoryboardDirector(
        OpenAIStoryboardConfig(api_key="test-key", model="gpt-5.6"), _client(handler)
    )
    result = await director.propose(request)

    schema = captured["body"]["text"]["format"]
    assert schema["type"] == "json_schema"
    assert schema["strict"] is True
    assert schema["schema"]["additionalProperties"] is False
    assert captured["key"] == request.idempotency_key
    assert captured["auth"] == "Bearer test-key"
    assert result.provider == "openai"
    assert result.model == "gpt-5.6"
    assert result.provider_request_id == "resp_storyboard_1"
    assert result.usage == {"input_tokens": 1200, "output_tokens": 900}
    assert result.attempt_number == 1
    assert result.proposals


@pytest.mark.asyncio
async def test_adapter_never_sends_credentials_or_trace_context_to_the_model() -> None:
    request = _request()
    body = await _director_payload(request)
    captured: dict[str, Any] = {}

    async def handler(http_request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(http_request.content)
        return httpx.Response(
            200,
            json={
                "id": "resp_storyboard_2",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": json.dumps(body)}],
                    }
                ],
            },
        )

    director = OpenAIStoryboardDirector(
        OpenAIStoryboardConfig(api_key="super-secret"), _client(handler)
    )
    result = await director.propose(request)
    user_content = captured["body"]["input"][1]["content"]
    assert "super-secret" not in user_content
    assert "traceparent" not in user_content
    # Provider metadata stays bounded and separate from canonical output.
    assert set(result.redacted_response_metadata) == {
        "status",
        "prompt_version",
        "configuration_version",
    }


@pytest.mark.asyncio
async def test_adapter_rejects_a_refusal_and_an_incomplete_response() -> None:
    async def refusal(http_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "resp_refusal",
                "status": "completed",
                "output": [{"type": "message", "content": [{"type": "refusal"}]}],
            },
        )

    director = OpenAIStoryboardDirector(
        OpenAIStoryboardConfig(api_key="test-key"), _client(refusal)
    )
    with pytest.raises(ValueError, match="refused"):
        await director.propose(_request())

    with pytest.raises(ValueError, match="incomplete"):
        response_text({"status": "incomplete"})
    with pytest.raises(ValueError, match="no output text"):
        response_text({"status": "completed", "output": []})


def test_strict_schema_inlines_definitions_and_forbids_extra_properties() -> None:
    from services.storyboard.openai_adapter import _output_schema

    schema = strict_schema(_output_schema())
    rendered = json.dumps(schema)
    # Strict mode rejects $ref, so every definition must be inlined.
    assert "$ref" not in rendered
    assert "$defs" not in rendered
    assert schema["required"] == list(schema["properties"])
    assert set(schema["properties"]) == {
        "proposals",
        "expected_incoming_continuity",
        "expected_outgoing_continuity",
        "warnings",
    }

    def object_depth(value: Any, depth: int = 0) -> int:
        if isinstance(value, dict):
            nested = depth + (1 if value.get("type") == "object" else 0)
            return max([object_depth(item, nested) for item in value.values()] or [depth])
        if isinstance(value, list):
            return max([object_depth(item, depth) for item in value] or [depth])
        return depth

    # Strict structured outputs cap nesting; stay inside the documented limit.
    assert object_depth(schema) <= 5


def test_adapter_requires_an_api_key() -> None:
    with pytest.raises(ValueError, match="API key"):
        OpenAIStoryboardDirector(OpenAIStoryboardConfig(api_key=""))
