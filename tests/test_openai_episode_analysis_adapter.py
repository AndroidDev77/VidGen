import json
from uuid import uuid4

import httpx
import pytest

from services.analysis.openai_adapter import (
    OpenAIAnalysisConfig,
    OpenAIEpisodeAnalysisProvider,
    _prompt,
)
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


def _prompt_text(filename: str) -> str:
    """The prompt with its hard line wraps collapsed so phrases match verbatim."""
    return " ".join(_prompt(filename, "episode-analysis-v1").split())


@pytest.mark.parametrize("filename", ["episode_scene_v1.txt", "episode_reduce_v1.txt"])
def test_analysis_prompts_demand_random_globally_unique_uuids(filename: str) -> None:
    """Regression guard for DUPLICATE_ID at reduce time.

    Left to its own devices the model emitted sequential, patterned UUIDs, and
    every chunk of a multi-chunk run produced the same sequence, so the reduce
    step failed validation. The prompts must keep demanding random v4 values
    that are unique across the whole output.
    """
    prompt = _prompt_text(filename)
    assert "UUID v4" in prompt
    assert "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx" in prompt
    assert "Never use sequential, patterned, or repeated" in prompt
    assert "no two entities of any type may share a UUID" in prompt


@pytest.mark.parametrize("filename", ["episode_scene_v1.txt", "episode_reduce_v1.txt"])
def test_analysis_prompts_forbid_aliases_that_repeat_the_canonical_name(filename: str) -> None:
    """Regression guard for UNSUPPORTED_ALIAS_MERGE."""
    prompt = _prompt_text(filename)
    assert "never add an alias that duplicates the canonical_name" in prompt
    assert "alias_evidence" in prompt


@pytest.mark.parametrize("filename", ["episode_scene_v1.txt", "episode_reduce_v1.txt"])
def test_analysis_prompts_require_upstream_ids_to_be_copied(filename: str) -> None:
    """Regression guard for SCENE_SET_MISMATCH and UNKNOWN_SOURCE_REFERENCE.

    The model read the "randomly generated UUID v4" rule as covering scene_id
    and reference_id too, and minted fresh ones instead of copying the values it
    was handed, so validation rejected the output. The prompts must scope
    generation to the IDs the step actually introduces.
    """
    prompt = _prompt_text(filename)
    assert "Copy IDs that already exist in the input; never generate them." in prompt
    assert "never to scene_id or reference_id" in prompt
    assert "Random UUID v4 generation applies only to" in prompt


def test_the_reduce_prompt_names_the_ids_it_may_generate() -> None:
    prompt = _prompt_text("episode_reduce_v1.txt")
    generated = prompt.split("Random UUID v4 generation applies only to", 1)[1]
    generated = generated.split("Each of those", 1)[0]
    for field in (
        "character_id",
        "location_id",
        "state_event_id",
        "relationship_id",
        "plot_beat_id",
        "ambiguity_id",
    ):
        assert field in generated
    assert "scene_id," not in generated
    assert "reference_id," not in generated
    assert "must be copied character for character from the scene_id" in prompt
    assert (
        "Every reference_id in every source_references list must be copied character for "
        "character from a reference_id that appears in the input scene results" in prompt
    )


def test_the_reduce_prompt_requires_uniqueness_across_input_chunks() -> None:
    prompt = _prompt_text("episode_reduce_v1.txt")
    assert "even if they come from different input chunks" in prompt


def test_an_unknown_prompt_version_is_refused() -> None:
    with pytest.raises(ValueError, match="unsupported prompt version"):
        _prompt("episode_scene_v1.txt", "episode-analysis-v2")
