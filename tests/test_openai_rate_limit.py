"""Backoff and spend-limit detection for OpenAI 429 responses.

A rate limit is waited out inside the adapter so a pipeline retry loop never
turns it into a tight resend loop; a spend limit is terminal and never waited
out at all.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest

from services.analysis.openai_adapter import OpenAIAnalysisConfig, OpenAIEpisodeAnalysisProvider
from services.analysis.provider import GenerationContext
from services.narration.openai_adapter import OpenAINarrationProvider
from services.narration.providers import OPENAI_TTS_MODEL
from services.transcription.openai_adapter import OpenAITranscriptionAdapter
from vidgen.contracts.episode_analysis import (
    SceneAnalysisRequest,
    SceneAnalysisResult,
    SourceReference,
)
from vidgen.contracts.narration import NarrationProviderRequest
from vidgen.contracts.transcription import AudioChunk, TranscriptionRequest
from vidgen.db.cost_repository import BudgetExceededError
from vidgen.providers.openai_rate_limit import (
    OpenAIRateLimited,
    OpenAISpendLimitExceeded,
    RateLimitBackoff,
    parse_retry_after,
    retry_delay_for_rate_limit,
    send_with_backoff,
)

NO_JITTER = RateLimitBackoff(max_attempts=4, base_seconds=1, jitter_ratio=0)

SPEND_LIMIT_BODY: dict[str, Any] = {
    "error": {
        "message": "You exceeded your current quota, please check your plan and billing details.",
        "type": "insufficient_quota",
        "code": "insufficient_quota",
    }
}
RATE_LIMIT_BODY: dict[str, Any] = {
    "error": {
        "message": "Rate limit reached for gpt-5 in organization org-1 on requests per min.",
        "type": "requests",
        "code": "rate_limit_exceeded",
    }
}


def _rate_limited(headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(429, json=RATE_LIMIT_BODY, headers=headers or {})


def _recording_sleep(recorded: list[float]) -> Callable[[float], Any]:
    async def sleep(delay: float) -> None:
        recorded.append(delay)

    return sleep


def test_retry_after_accepts_seconds_and_http_dates() -> None:
    now = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
    assert parse_retry_after({"retry-after": "20"}) == 20
    assert parse_retry_after({"Retry-After": " 1.5 "}) == 1.5
    assert (
        parse_retry_after({"retry-after": format_datetime(now + timedelta(seconds=30))}, now=now)
        == 30
    )
    # A deadline that has already passed means "retry now", not "wait forever".
    assert (
        parse_retry_after({"retry-after": format_datetime(now - timedelta(seconds=30))}, now=now)
        == 0
    )
    assert parse_retry_after({}) is None
    assert parse_retry_after({"retry-after": "soon"}) is None


def test_a_rate_limit_waits_for_the_retry_after_header() -> None:
    response = _rate_limited({"retry-after": "17"})
    assert retry_delay_for_rate_limit(response, attempt=1, policy=NO_JITTER) == 17


def test_jitter_only_ever_lengthens_the_wait_the_provider_asked_for() -> None:
    response = _rate_limited({"retry-after": "10"})
    policy = RateLimitBackoff(jitter_ratio=0.25)
    delays = [retry_delay_for_rate_limit(response, attempt=1, policy=policy) for _ in range(50)]
    assert all(10 <= delay <= 12.5 for delay in delays)
    # Scenes rate limited together by one asyncio.gather must not all wake at
    # the same instant, so the jitter has to actually vary.
    assert len(set(delays)) > 1


def test_a_rate_limit_without_a_header_backs_off_exponentially() -> None:
    response = _rate_limited()
    delays = [retry_delay_for_rate_limit(response, attempt=n, policy=NO_JITTER) for n in (1, 2, 3)]
    assert delays == [1, 2, 4]


def test_the_backoff_is_capped() -> None:
    policy = RateLimitBackoff(max_attempts=20, base_seconds=1, max_seconds=30, jitter_ratio=0)
    assert retry_delay_for_rate_limit(_rate_limited(), attempt=10, policy=policy) == 30
    assert (
        retry_delay_for_rate_limit(_rate_limited({"retry-after": "600"}), attempt=1, policy=policy)
        == 30
    )


def test_an_exhausted_rate_limit_reports_the_provider_delay() -> None:
    with pytest.raises(OpenAIRateLimited) as raised:
        retry_delay_for_rate_limit(_rate_limited({"retry-after": "9"}), attempt=4, policy=NO_JITTER)
    assert raised.value.retry_after == 9


@pytest.mark.parametrize(
    "body",
    [
        SPEND_LIMIT_BODY,
        {"error": {"message": "Billing hard limit has been reached", "type": "server_error"}},
        {"error": {"message": "no code here", "code": "billing_hard_limit_reached"}},
    ],
)
def test_a_spend_limit_is_terminal(body: dict[str, Any]) -> None:
    response = httpx.Response(429, json=body, headers={"retry-after": "1"})
    with pytest.raises(OpenAISpendLimitExceeded):
        retry_delay_for_rate_limit(response, attempt=1, policy=NO_JITTER)


def test_a_spend_limit_is_a_budget_denial() -> None:
    # The pipelines already stop on BudgetExceededError; a provider spend limit
    # rides that branch instead of burning the stage's remaining attempts.
    assert issubclass(OpenAISpendLimitExceeded, BudgetExceededError)


def test_an_unreadable_or_unrelated_body_is_treated_as_a_passing_rate_limit() -> None:
    for response in (
        httpx.Response(429, content=b"<html>too many requests</html>"),
        httpx.Response(429, json={"error": "rate limited"}),
        httpx.Response(429, json=RATE_LIMIT_BODY),
    ):
        assert retry_delay_for_rate_limit(response, attempt=1, policy=NO_JITTER) == 1


@pytest.mark.asyncio
async def test_send_with_backoff_waits_then_returns_the_successful_response() -> None:
    responses = [_rate_limited({"retry-after": "3"}), _rate_limited(), httpx.Response(200)]
    sent: list[int] = []
    slept: list[float] = []

    async def send() -> httpx.Response:
        sent.append(len(sent))
        return responses[len(sent) - 1]

    result = await send_with_backoff(send, policy=NO_JITTER, sleep=_recording_sleep(slept))
    assert result.status_code == 200
    assert slept == [3, 2]
    assert len(sent) == 3


@pytest.mark.asyncio
async def test_send_with_backoff_gives_up_after_the_policy_attempts() -> None:
    sent: list[int] = []
    slept: list[float] = []

    async def send() -> httpx.Response:
        sent.append(len(sent))
        return _rate_limited()

    with pytest.raises(OpenAIRateLimited):
        await send_with_backoff(send, policy=NO_JITTER, sleep=_recording_sleep(slept))
    assert len(sent) == NO_JITTER.max_attempts
    assert slept == [1, 2, 4]


@pytest.mark.asyncio
async def test_send_with_backoff_fails_fast_on_a_spend_limit() -> None:
    sent: list[int] = []
    slept: list[float] = []

    async def send() -> httpx.Response:
        sent.append(len(sent))
        return httpx.Response(429, json=SPEND_LIMIT_BODY, headers={"retry-after": "60"})

    with pytest.raises(OpenAISpendLimitExceeded):
        await send_with_backoff(send, policy=NO_JITTER, sleep=_recording_sleep(slept))
    assert len(sent) == 1
    assert slept == []


@pytest.mark.asyncio
async def test_other_statuses_are_returned_untouched_for_the_caller_to_raise() -> None:
    slept: list[float] = []

    async def send() -> httpx.Response:
        return httpx.Response(500, text="boom")

    response = await send_with_backoff(send, policy=NO_JITTER, sleep=_recording_sleep(slept))
    assert response.status_code == 500
    assert slept == []


def _scene_request() -> SceneAnalysisRequest:
    scene_id = uuid4()
    return SceneAnalysisRequest(
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
        evidence_references=[
            SourceReference(
                reference_type="source_scene",
                reference_id=scene_id,
                scene_id=scene_id,
                start_ms=0,
                end_ms=1000,
            )
        ],
    )


def _scene_payload(request: SceneAnalysisRequest) -> dict[str, Any]:
    output = SceneAnalysisResult(
        scene_id=request.scene_id,
        sequence=1,
        source_start_ms=0,
        source_end_ms=1000,
        summary="Observed",
        dramatic_purpose="Chronology",
        confidence=1,
        source_references=list(request.evidence_references),
    )
    return {
        "id": "resp_1",
        "status": "completed",
        "output": [{"content": [{"type": "output_text", "text": output.model_dump_json()}]}],
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


@pytest.mark.asyncio
async def test_the_analysis_adapter_waits_out_a_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _scene_request()
    slept: list[float] = []
    monkeypatch.setattr(asyncio, "sleep", _recording_sleep(slept))
    attempts: list[httpx.Request] = []

    async def handler(http_request: httpx.Request) -> httpx.Response:
        attempts.append(http_request)
        if len(attempts) == 1:
            return _rate_limited({"retry-after": "4"})
        return httpx.Response(200, json=_scene_payload(request))

    client = httpx.AsyncClient(
        base_url="https://api.openai.test/v1", transport=httpx.MockTransport(handler)
    )
    provider = OpenAIEpisodeAnalysisProvider(
        OpenAIAnalysisConfig(api_key="k", model="gpt-5"), client=client
    )
    result = await provider.analyze_scene(request, GenerationContext(attempt_number=1))
    await client.aclose()
    assert result.output.summary == "Observed"
    assert len(attempts) == 2
    # The retried call keeps the idempotency key, so the wait never buys a
    # second billed generation.
    assert {item.headers["idempotency-key"] for item in attempts} == {"scene-key"}
    assert slept and slept[0] >= 4


@pytest.mark.asyncio
async def test_the_analysis_adapter_fails_fast_on_a_spend_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slept: list[float] = []
    monkeypatch.setattr(asyncio, "sleep", _recording_sleep(slept))
    attempts: list[httpx.Request] = []

    async def handler(http_request: httpx.Request) -> httpx.Response:
        attempts.append(http_request)
        return httpx.Response(429, json=SPEND_LIMIT_BODY)

    client = httpx.AsyncClient(
        base_url="https://api.openai.test/v1", transport=httpx.MockTransport(handler)
    )
    provider = OpenAIEpisodeAnalysisProvider(
        OpenAIAnalysisConfig(api_key="k", model="gpt-5"), client=client
    )
    with pytest.raises(OpenAISpendLimitExceeded):
        await provider.analyze_scene(_scene_request(), GenerationContext(attempt_number=1))
    await client.aclose()
    assert len(attempts) == 1
    assert slept == []


@pytest.mark.asyncio
async def test_the_transcription_adapter_reopens_the_upload_on_each_attempt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    slept: list[float] = []
    monkeypatch.setattr(asyncio, "sleep", _recording_sleep(slept))
    bodies: list[bytes] = []

    async def handler(http_request: httpx.Request) -> httpx.Response:
        bodies.append(await http_request.aread())
        if len(bodies) == 1:
            return _rate_limited({"retry-after": "2"})
        return httpx.Response(200, json={"text": "hello", "language": "en", "duration": 1})

    client = httpx.AsyncClient(
        base_url="https://api.openai.test/v1", transport=httpx.MockTransport(handler)
    )
    adapter = OpenAITranscriptionAdapter(api_key="k", client=client)
    audio = tmp_path / "chunk.flac"
    audio.write_bytes(b"fake-flac")
    chunk = AudioChunk(
        asset_id=uuid4(),
        parent_audio_asset_id=uuid4(),
        sequence=0,
        start_seconds=0,
        end_seconds=1,
        byte_size=9,
        sha256="c" * 64,
        codec="flac",
        sample_rate=16_000,
        idempotency_key="chunk",
    )
    result = await adapter.transcribe(TranscriptionRequest(idempotency_key="t", chunk=chunk), audio)
    await client.aclose()
    assert result.text == "hello"
    # The retry re-reads the file: a rewound-once stream would send an empty part.
    assert [body.count(b"fake-flac") for body in bodies] == [1, 1]


def _narration_request() -> NarrationProviderRequest:
    return NarrationProviderRequest(
        idempotency_key="stable",
        project_id=uuid4(),
        script_id=uuid4(),
        script_version=1,
        script_segment_id=uuid4(),
        segment_sequence=0,
        text="One line of narration.",
        voice_profile_id=uuid4(),
        voice_profile_version=1,
        voice_id="cedar",
        model=OPENAI_TTS_MODEL,
        output_format="wav",
        language="en",
        attempt_number=1,
    )


@pytest.mark.asyncio
async def test_the_streaming_narration_adapter_waits_out_a_rate_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    slept: list[float] = []
    monkeypatch.setattr(asyncio, "sleep", _recording_sleep(slept))
    attempts: list[httpx.Request] = []

    async def handler(http_request: httpx.Request) -> httpx.Response:
        attempts.append(http_request)
        if len(attempts) == 1:
            return _rate_limited({"retry-after": "5"})
        return httpx.Response(
            200, content=b"audio-bytes", headers={"content-type": "audio/wav", "x-request-id": "r1"}
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAINarrationProvider("k", client=client, backoff=NO_JITTER)
    destination = tmp_path / "narration.wav"
    result = await provider.generate(_narration_request(), destination)
    await client.aclose()
    assert destination.read_bytes() == b"audio-bytes"
    assert result.provider_request_id == "r1"
    assert slept == [5]
    assert len(attempts) == 2


@pytest.mark.asyncio
async def test_the_streaming_narration_adapter_fails_fast_on_a_spend_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    slept: list[float] = []
    monkeypatch.setattr(asyncio, "sleep", _recording_sleep(slept))
    attempts: list[httpx.Request] = []

    async def handler(http_request: httpx.Request) -> httpx.Response:
        attempts.append(http_request)
        return httpx.Response(429, content=json.dumps(SPEND_LIMIT_BODY).encode())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAINarrationProvider("k", client=client, backoff=NO_JITTER)
    with pytest.raises(OpenAISpendLimitExceeded):
        await provider.generate(_narration_request(), tmp_path / "narration.wav")
    await client.aclose()
    assert len(attempts) == 1
    assert slept == []
