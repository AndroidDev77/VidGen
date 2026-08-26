import json
from uuid import uuid4

import httpx
import pytest

from services.analysis.openai_adapter import OpenAIAnalysisConfig, OpenAIEpisodeAnalysisProvider
from services.analysis.provider import GenerationContext
from vidgen.contracts.episode_analysis import (
    SceneAnalysisRequest,
    SceneAnalysisResult,
    SourceReference,
)


@pytest.mark.asyncio
async def test_openai_scene_request_uses_strict_schema_and_parses_raw_response() -> None:
    scene_id = uuid4()
    reference = SourceReference(
        reference_type="source_scene",
        reference_id=scene_id,
        scene_id=scene_id,
        start_ms=0,
        end_ms=1000,
    )
    request = SceneAnalysisRequest(
        project_id=uuid4(),
        evidence_package_id=uuid4(),
        scene_id=scene_id,
        sequence=1,
        source_start_ms=0,
        source_end_ms=1000,
        input_hash="a" * 64,
        idempotency_key="scene-key",
        contract_version="1.0",
        prompt_version="episode-analysis-v1",
        provider_configuration_version="test",
        evidence_references=[reference],
    )
    output = SceneAnalysisResult(
        scene_id=scene_id,
        sequence=1,
        source_start_ms=0,
        source_end_ms=1000,
        summary="Observed",
        dramatic_purpose="Chronology",
        confidence=1,
        source_references=[reference],
    )
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
                "usage": {"input_tokens": 2, "output_tokens": 3},
            },
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://example.test"
    )
    provider = OpenAIEpisodeAnalysisProvider(
        OpenAIAnalysisConfig(
            api_key="not-real", model="configured-model", base_url="https://example.test"
        ),
        client,
    )
    result = await provider.analyze_scene(
        request, GenerationContext(validation_errors_json='{"errors":[]}')
    )
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["model"] == "configured-model" and captured["key"] == "scene-key"
    schema = body["text"]["format"]["schema"]
    assert schema["additionalProperties"] is False
    assert "Validation errors to repair" in body["input"][1]["content"]
    assert result.output == output and result.metadata.provider_request_id == "resp_fake"
    await client.aclose()
