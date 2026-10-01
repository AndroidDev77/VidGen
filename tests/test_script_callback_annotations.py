"""A stray ``callback_id`` on a non-callback joke must not cost a project run.

``JokeAnnotation.callback_requires_type`` is a cross-field rule a structured
output schema cannot carry, so a writer model can break it while satisfying
every field. The raw payload is normalised before the contract sees it, and a
payload the contract still refuses is re-asked rather than failing the run.
"""

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select

from services.script.canonicalize import JOKE_CALLBACK_NORMALIZED, normalize_raw_joke_callbacks
from services.script.compressor import compress_plot
from services.script.fake_provider import FakeScriptGenerationProvider
from services.script.openai_adapter import OpenAIScriptConfig, OpenAIScriptGenerationProvider
from services.script.pipeline import PROVIDER_PAYLOAD_INVALID, ScriptGenerationPipeline
from services.script.provider import GenerationContext
from services.script.writer import write_script
from tests.test_openai_script_adapter import _compression_request, _writing_request
from tests.test_script_pipeline import _database, _make_analysis
from vidgen.contracts.script import JokeAnnotation, RecapScript
from vidgen.db.cost_models import ProviderAttempt


def _sound_raw_script() -> tuple[Any, dict[str, Any]]:
    analysis = _make_analysis(uuid4())
    plan = compress_plot(analysis=analysis, request=_compression_request(analysis), plan_id=uuid4())
    request = _writing_request(analysis, plan)
    script = write_script(plan=plan, request=request, script_id=uuid4())
    assert script.callbacks, "the reference writer emits one callback"
    return request, json.loads(script.model_dump_json())


def _callback_joke(raw: dict[str, Any]) -> dict[str, Any]:
    payoff = next(
        segment
        for segment in raw["segments"]
        if segment["segment_id"] == raw["callbacks"][0]["payoff_segment_id"]
    )
    return next(item for item in payoff["joke_annotations"] if item["joke_type"] == "callback")


def _setup_joke(raw: dict[str, Any]) -> dict[str, Any]:
    setup = next(
        segment
        for segment in raw["segments"]
        if segment["segment_id"] == raw["callbacks"][0]["setup_segment_id"]
    )
    return setup["joke_annotations"][0]


def test_the_contract_still_refuses_the_unnormalised_payload() -> None:
    _request, raw = _sound_raw_script()
    _setup_joke(raw)["callback_id"] = raw["callbacks"][0]["callback_id"]
    with pytest.raises(ValidationError, match="only callback jokes may reference a callback_id"):
        RecapScript.model_validate(raw)


def test_a_mistyped_callback_payoff_is_retyped_as_callback() -> None:
    _request, raw = _sound_raw_script()
    joke = _callback_joke(raw)
    joke["joke_type"] = "commentary"

    normalize_raw_joke_callbacks(raw)

    assert joke["joke_type"] == "callback"
    assert joke["callback_id"] == raw["callbacks"][0]["callback_id"]
    assert [note["code"] for note in raw["warnings"]] == [JOKE_CALLBACK_NORMALIZED]
    assert "retyped as callback" in raw["warnings"][0]["message"]
    RecapScript.model_validate(raw)


@pytest.mark.parametrize(
    "case", ["setup_segment", "unknown_callback", "payoff_already_claimed", "uppercase_unknown"]
)
def test_a_callback_id_the_joke_does_not_pay_off_is_dropped(case: str) -> None:
    _request, raw = _sound_raw_script()
    callback_id = raw["callbacks"][0]["callback_id"]
    if case == "setup_segment":
        joke = _setup_joke(raw)
        joke["callback_id"] = callback_id
    elif case == "unknown_callback":
        joke = _setup_joke(raw)
        joke["callback_id"] = str(uuid4())
    elif case == "uppercase_unknown":
        joke = _setup_joke(raw)
        joke["callback_id"] = str(uuid4()).upper()
    else:
        payoff = next(
            segment
            for segment in raw["segments"]
            if segment["segment_id"] == raw["callbacks"][0]["payoff_segment_id"]
        )
        joke = next(item for item in payoff["joke_annotations"] if item["joke_type"] != "callback")
        joke["callback_id"] = callback_id
    mechanism = joke["joke_type"]

    normalize_raw_joke_callbacks(raw)

    assert joke["callback_id"] is None
    assert joke["joke_type"] == mechanism
    assert _callback_joke(raw)["callback_id"] == callback_id
    assert [note["code"] for note in raw["warnings"]] == [JOKE_CALLBACK_NORMALIZED]
    assert "callback_id removed" in raw["warnings"][0]["message"]
    RecapScript.model_validate(raw)


def test_a_consistent_payload_is_left_untouched() -> None:
    _request, raw = _sound_raw_script()
    before = json.loads(json.dumps(raw))
    normalize_raw_joke_callbacks(raw)
    assert raw == before
    # Shapes that are not a script are left for the contract to refuse.
    for junk in (None, [], {"segments": None}, {"segments": [None, {"joke_annotations": 3}]}):
        normalize_raw_joke_callbacks(junk)


@pytest.mark.asyncio
async def test_the_writer_adapter_turns_the_incident_payload_into_a_valid_script() -> None:
    """The run-killing payload: ``segments.N.joke_annotations.0`` with a stray id."""
    request, raw = _sound_raw_script()
    _setup_joke(raw)["callback_id"] = raw["callbacks"][0]["callback_id"]
    _callback_joke(raw)["joke_type"] = "wordplay"

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

    script = result.output
    assert isinstance(script, RecapScript)
    annotations = [item for segment in script.segments for item in segment.joke_annotations]
    linked = [item for item in annotations if item.callback_id is not None]
    assert [(item.joke_type, item.callback_id) for item in linked] == [
        ("callback", script.callbacks[0].callback_id)
    ]
    assert [note.code for note in script.warnings] == [JOKE_CALLBACK_NORMALIZED] * 2


def test_the_writer_prompt_states_the_callback_rule() -> None:
    prompt = (
        Path(__file__).parents[1] / "services/script/prompts/comedy_writer_v1.txt"
    ).read_text()
    assert "only when its `joke_type` is `callback`" in prompt


class _OnceInvalidDraftProvider(FakeScriptGenerationProvider):
    """Raises the contract's ValidationError for the first ``failures`` drafts."""

    def __init__(self, failures: int = 1) -> None:
        super().__init__()
        self.failures = failures
        self.contexts: list[GenerationContext] = []

    async def write_script(self, request, context):  # type: ignore[override]
        self.contexts.append(context)
        result = await super().write_script(request, context)
        if len(self.contexts) <= self.failures:
            raw = json.loads(result.output.model_dump_json())
            raw["segments"][0]["joke_annotations"][0]["callback_id"] = str(uuid4())
            RecapScript.model_validate(raw)
        return result


@pytest.mark.asyncio
async def test_a_draft_the_contract_refuses_is_re_asked_not_fatal(tmp_path: Path) -> None:
    session, blobs, project, _record = _database(tmp_path)
    provider = _OnceInvalidDraftProvider()
    result = await ScriptGenerationPipeline(session, blobs, provider).process(
        project_id=project.id, idempotency_key="run-1"
    )
    assert result.status == "script_review_required"

    assert len(provider.contexts) == 2
    assert provider.contexts[0].validation_errors_json is None
    feedback = provider.contexts[1].validation_errors_json
    assert feedback is not None
    assert "only callback jokes may reference a callback_id" in feedback

    attempts = session.scalars(
        select(ProviderAttempt)
        .where(ProviderAttempt.operation == "script.write_script")
        .order_by(ProviderAttempt.attempt_number)
    ).all()
    assert [(row.status, row.error_code) for row in attempts] == [
        ("FAILED", PROVIDER_PAYLOAD_INVALID),
        ("SUCCEEDED", None),
    ]
    assert attempts[0].failure_class == "CONTRACT_VALIDATION"


@pytest.mark.asyncio
async def test_the_re_ask_is_bounded_by_the_repair_budget(tmp_path: Path) -> None:
    session, blobs, project, _record = _database(tmp_path)
    provider = _OnceInvalidDraftProvider(failures=2)
    with pytest.raises(ValidationError):
        await ScriptGenerationPipeline(session, blobs, provider, max_repair_attempts=2).process(
            project_id=project.id, idempotency_key="run-1"
        )
    assert len(provider.contexts) == 2


def test_joke_annotation_contract_is_unchanged() -> None:
    with pytest.raises(ValidationError):
        JokeAnnotation(
            joke_id=uuid4(),
            joke_type="commentary",
            callback_id=uuid4(),
            source_beat_ids=[uuid4()],
        )
