import json
from uuid import uuid4

import httpx
import pytest

from services.script.compressor import compress_plot
from services.script.openai_adapter import OpenAIScriptConfig, OpenAIScriptGenerationProvider
from services.script.provider import GenerationContext
from tests.test_script_pipeline import _make_analysis
from vidgen.contracts.script import PlotCompressionRequest


def _compression_request(analysis) -> PlotCompressionRequest:
    return PlotCompressionRequest(
        project_id=analysis.project_id,
        episode_analysis_id=analysis.episode_id,
        episode_analysis=analysis,
        input_hash="a" * 64,
        idempotency_key="compress-key",
        contract_version="1.0",
        prompt_version="comedy-script-v1",
        provider_configuration_version="openai-script-responses-v1",
        target_duration_ms=240_000,
        target_words=600,
        target_words_per_minute=150,
        recap_mode="full_recap",
    )


@pytest.mark.asyncio
async def test_openai_compress_plot_uses_strict_schema_and_parses_response() -> None:
    analysis = _make_analysis(uuid4())
    request = _compression_request(analysis)
    output = compress_plot(analysis=analysis, request=request, plan_id=uuid4())
    captured: dict[str, object] = {}

    async def handler(http_request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(http_request.content)
        captured["key"] = http_request.headers["Idempotency-Key"]
        return httpx.Response(
            200,
            json={
                "id": "resp_fake",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": output.model_dump_json()}],
                    }
                ],
                "usage": {"input_tokens": 4, "output_tokens": 8},
            },
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://example.test"
    )
    provider = OpenAIScriptGenerationProvider(
        OpenAIScriptConfig(
            api_key="not-real",
            compressor_model="configured-compressor",
            writer_model="configured-writer",
            editor_model="configured-editor",
            base_url="https://example.test",
        ),
        client,
    )
    result = await provider.compress_plot(request, GenerationContext())
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["model"] == "configured-compressor"
    assert captured["key"] == "compress-key"
    schema = body["text"]["format"]["schema"]
    assert schema["additionalProperties"] is False
    assert result.output == output
    assert result.metadata.provider_request_id == "resp_fake"
    assert result.metadata.operation == "compress_plot"
    assert result.metadata.input_tokens == 4
    await client.aclose()


@pytest.mark.asyncio
async def test_openai_adapter_includes_repair_feedback_in_user_message() -> None:
    analysis = _make_analysis(uuid4())
    request = _compression_request(analysis)
    output = compress_plot(analysis=analysis, request=request, plan_id=uuid4())
    captured: dict[str, object] = {}

    async def handler(http_request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(http_request.content)
        return httpx.Response(
            200,
            json={
                "id": "resp_repair",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": output.model_dump_json()}],
                    }
                ],
            },
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://example.test"
    )
    provider = OpenAIScriptGenerationProvider(
        OpenAIScriptConfig(
            api_key="not-real",
            compressor_model="m",
            writer_model="m",
            editor_model="m",
            base_url="https://example.test",
        ),
        client,
    )
    await provider.compress_plot(
        request, GenerationContext(attempt_number=2, validation_errors_json='{"errors":["x"]}')
    )
    body = captured["body"]
    assert isinstance(body, dict)
    assert "Validation errors to repair" in body["input"][1]["content"]
    await client.aclose()


@pytest.mark.asyncio
async def test_openai_adapter_raises_on_malformed_json_response() -> None:
    analysis = _make_analysis(uuid4())
    request = _compression_request(analysis)

    async def handler(_http_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "resp_bad",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "{not valid json"}],
                    }
                ],
            },
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://example.test"
    )
    provider = OpenAIScriptGenerationProvider(
        OpenAIScriptConfig(
            api_key="not-real",
            compressor_model="m",
            writer_model="m",
            editor_model="m",
            base_url="https://example.test",
        ),
        client,
    )
    with pytest.raises(json.JSONDecodeError):
        await provider.compress_plot(request, GenerationContext())
    await client.aclose()


@pytest.mark.asyncio
async def test_openai_adapter_raises_on_refusal() -> None:
    analysis = _make_analysis(uuid4())
    request = _compression_request(analysis)

    async def handler(_http_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "resp_refused",
                "status": "completed",
                "output": [{"type": "message", "content": [{"type": "refusal"}]}],
            },
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://example.test"
    )
    provider = OpenAIScriptGenerationProvider(
        OpenAIScriptConfig(
            api_key="not-real",
            compressor_model="m",
            writer_model="m",
            editor_model="m",
            base_url="https://example.test",
        ),
        client,
    )
    with pytest.raises(ValueError, match="refused"):
        await provider.compress_plot(request, GenerationContext())
    await client.aclose()


@pytest.mark.asyncio
async def test_openai_adapter_propagates_transport_timeout() -> None:
    analysis = _make_analysis(uuid4())
    request = _compression_request(analysis)

    async def handler(_http_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out")

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://example.test"
    )
    provider = OpenAIScriptGenerationProvider(
        OpenAIScriptConfig(
            api_key="not-real",
            compressor_model="m",
            writer_model="m",
            editor_model="m",
            base_url="https://example.test",
        ),
        client,
    )
    with pytest.raises(httpx.ConnectTimeout):
        await provider.compress_plot(request, GenerationContext())
    await client.aclose()
