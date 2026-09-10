import json
from uuid import uuid4

import httpx
import pytest

from services.script.canonicalize import EMPTY_SEGMENT_DROPPED
from services.script.compressor import compress_plot
from services.script.openai_adapter import (
    OpenAIScriptConfig,
    OpenAIScriptGenerationProvider,
    _drop_empty_segments,
)
from services.script.provider import GenerationContext
from services.script.writer import write_script
from tests.test_script_pipeline import _make_analysis
from vidgen.contracts.script import (
    ChannelVoiceConfig,
    ComedyWritingRequest,
    PlotCompressionRequest,
)


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


def _script_prompt_text(filename: str) -> str:
    """The prompt with its hard line wraps collapsed so phrases match verbatim."""
    from services.script.openai_adapter import _prompt

    return " ".join(_prompt(filename).split())


def test_the_compressor_prompt_requires_reference_ids_to_be_copied() -> None:
    """Regression guard for UNKNOWN_SOURCE_REFERENCE at compression time.

    The ID-copy rule named plot beat, scene, character and relationship IDs but
    not reference_id, so the model minted fresh ones for source_references and
    the plan failed validation.
    """
    prompt = _script_prompt_text("plot_compressor_v1.txt")
    assert (
        "Every reference_id in every source_references list must also be copied exactly "
        "from the input — never invent a reference_id." in prompt
    )


def _writing_request(analysis, plan) -> ComedyWritingRequest:
    return ComedyWritingRequest(
        project_id=analysis.project_id,
        episode_analysis_id=analysis.episode_id,
        compressed_plot_plan_id=plan.plan_id,
        input_hash="a" * 64,
        idempotency_key="write-key",
        contract_version="1.0",
        prompt_version="comedy-script-v1",
        provider_configuration_version="openai-script-responses-v1",
        compressed_plot=plan,
        channel_voice=ChannelVoiceConfig(narrator_persona="wry narrator"),
        humor_intensity=0.6,
        target_words=600,
        recap_mode="full_recap",
    )


@pytest.mark.asyncio
async def test_openai_write_script_clears_empty_beats_from_the_response() -> None:
    """A blank NARRATION segment would fail the contract; a blank PAUSE would pass it.

    Both are removed before validation and recorded on the script's warnings,
    so the pipeline receives a script it can persist and narrate.
    """
    analysis = _make_analysis(uuid4())
    plan = compress_plot(analysis=analysis, request=_compression_request(analysis), plan_id=uuid4())
    request = _writing_request(analysis, plan)
    sound = write_script(plan=plan, request=request, script_id=uuid4())
    raw = json.loads(sound.model_dump_json())
    last = raw["segments"][-1]
    raw["segments"].extend(
        [
            {**last, "segment_id": str(uuid4()), "sequence": last["sequence"] + 1, "text": ""},
            {
                **last,
                "segment_id": str(uuid4()),
                "sequence": last["sequence"] + 2,
                "type": "PAUSE",
                "text": "  ",
                "joke_annotations": [],
            },
        ]
    )

    async def handler(_http_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "resp_fake",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": json.dumps(raw)}],
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
    result = await provider.write_script(request, GenerationContext())
    await client.aclose()
    assert [segment.segment_id for segment in result.output.segments] == [
        segment.segment_id for segment in sound.segments
    ]
    assert all(segment.text.strip() for segment in result.output.segments)
    assert [note.code for note in result.output.warnings] == [
        EMPTY_SEGMENT_DROPPED,
        EMPTY_SEGMENT_DROPPED,
    ]
    assert result.output.warnings[0].message.startswith("NARRATION segment ")
    assert result.output.warnings[1].message.startswith("PAUSE segment ")


def test_the_raw_patch_leaves_a_script_without_empty_beats_alone() -> None:
    raw = {"segments": [{"type": "NARRATION", "text": "words"}], "warnings": []}
    _drop_empty_segments(raw)
    assert raw == {"segments": [{"type": "NARRATION", "text": "words"}], "warnings": []}
    # A response that is empty throughout is left for the contract to refuse.
    all_blank = {"segments": [{"type": "PAUSE", "text": ""}]}
    _drop_empty_segments(all_blank)
    assert all_blank == {"segments": [{"type": "PAUSE", "text": ""}]}
