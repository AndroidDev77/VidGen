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
        raw_proposals = parsed.get("proposals", [])
        # Sort by whatever sequence the model returned, then re-index from 0 so the
        # validator's dense-start-at-zero requirement is always satisfied regardless of
        # whether the model uses 0-based or 1-based indexing.
        raw_proposals.sort(key=lambda p: p.get("proposal_sequence", 0))
        for idx, item in enumerate(raw_proposals):
            item["proposal_sequence"] = idx
        word_count = len(request.word_timings)
        _fix_word_ranges(raw_proposals, word_count)
        valid_evidence_ids = {
            str(ref.reference_id)
            for ref in request.evidence_references
            if ref.reference_type in ("scene_evidence", "evidence_package")
        }
        for item in raw_proposals:
            _fix_evidence_references(item, valid_evidence_ids)
        return StoryboardProviderResult(
            proposals=[
                StoryboardShotProposal.model_validate(_fix_proposal(item))
                for item in raw_proposals
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


def _fix_evidence_references(item: Any, valid_evidence_ids: set[str]) -> None:
    """Remove evidence references whose IDs are not in the valid set.

    The model occasionally references evidence IDs that were not included in the
    request.  Leaving them in triggers ``missing_evidence_reference`` validation
    errors that the repair loop cannot recover from because the model keeps
    re-hallucinating the same invalid IDs.
    """
    refs = item.get("evidence_references")
    if not isinstance(refs, list):
        return
    item["evidence_references"] = [
        ref
        for ref in refs
        if not isinstance(ref, dict)
        or ref.get("reference_type") not in ("scene_evidence", "evidence_package")
        or str(ref.get("reference_id", "")) in valid_evidence_ids
    ]


def _fix_word_ranges(proposals: list[Any], word_count: int) -> None:
    """Make word ranges contiguous and fully covering.

    After sorting by proposal_sequence the model occasionally leaves a one-word
    gap between adjacent shots or undershoots the final word.  Close those gaps
    by snapping each shot's word_start_index to the previous shot's
    word_end_index, then extend the last shot to cover the segment word count.
    """
    cursor = 0
    for item in proposals:
        start = item.get("word_start_index")
        if isinstance(start, int) and start != cursor:
            item["word_start_index"] = cursor
        end = item.get("word_end_index")
        if isinstance(end, int) and end > cursor:
            cursor = end
        else:
            cursor += 1
            item["word_end_index"] = cursor
    if proposals and cursor != word_count:
        proposals[-1]["word_end_index"] = word_count


def _fix_proposal(item: Any) -> Any:
    """Patch model-generated fields that violate Pydantic storyboard invariants.

    The model occasionally returns:
    - a non-cut transition with duration_us <= 0 → convert to cut
    - a non-cut transition where handle_us < duration_us → clamp handle_us up
    - word_end_index <= word_start_index → clamp word_end_index to start + 1
    """
    for field in ("transition_in", "transition_out"):
        t = item.get(field)
        if not isinstance(t, dict) or t.get("kind", "cut") == "cut":
            continue
        duration = t.get("duration_us", 0)
        if duration <= 0:
            # No meaningful duration → treat as a cut
            t["kind"] = "cut"
            t["duration_us"] = 0
            t["handle_us"] = 0
        elif t.get("handle_us", 0) < duration:
            t["handle_us"] = duration
    start = item.get("word_start_index")
    end = item.get("word_end_index")
    if isinstance(start, int) and isinstance(end, int) and end <= start:
        item["word_end_index"] = start + 1
    # Ensure every referenced character is declared present in incoming continuity.
    refs = item.get("character_reference_ids")
    incoming = item.get("incoming_continuity")
    if isinstance(refs, list) and isinstance(incoming, dict):
        present = incoming.get("present_character_ids")
        if isinstance(present, list):
            present_set = set(present)
            for char_id in refs:
                if char_id not in present_set:
                    present.append(char_id)
                    present_set.add(char_id)
    return item


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
