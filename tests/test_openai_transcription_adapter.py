from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from services.transcription.openai_adapter import OpenAITranscriptionAdapter
from vidgen.contracts.transcription import AudioChunk, DiarizationRequest, TranscriptionRequest


def _chunk() -> AudioChunk:
    return AudioChunk(
        asset_id=uuid4(),
        parent_audio_asset_id=uuid4(),
        sequence=0,
        start_seconds=10,
        end_seconds=12,
        byte_size=10,
        sha256="c" * 64,
        codec="flac",
        sample_rate=16_000,
        idempotency_key="chunk",
    )


@pytest.mark.asyncio
async def test_openai_adapter_uses_configured_models_and_parses_timestamps(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = await request.aread()
        if b"diarized_json" in body:
            payload = {"segments": [{"speaker": "A", "start": 0, "end": 2, "text": "hello"}]}
        else:
            payload = {
                "text": "hello world",
                "language": "en",
                "duration": 2,
                "words": [
                    {"word": "hello", "start": 0, "end": 1},
                    {"word": "world", "start": 1, "end": 2},
                ],
                "segments": [{"text": "hello world", "start": 0, "end": 2}],
            }
        return httpx.Response(
            200, json=payload, headers={"x-request-id": f"request-{len(requests)}"}
        )

    client = httpx.AsyncClient(
        base_url="https://api.openai.test/v1", transport=httpx.MockTransport(handler)
    )
    adapter = OpenAITranscriptionAdapter(
        api_key="test-key",
        transcription_model="whisper-1",
        diarization_model="gpt-4o-transcribe-diarize",
        client=client,
    )
    audio = tmp_path / "chunk.flac"
    audio.write_bytes(b"fake-flac")
    chunk = _chunk()
    transcription = await adapter.transcribe(
        TranscriptionRequest(idempotency_key="t", chunk=chunk, language_hint="en"), audio
    )
    diarization = await adapter.diarize(DiarizationRequest(idempotency_key="d", chunk=chunk), audio)
    await client.aclose()
    assert [word.start_seconds for word in transcription.words] == [10, 11]
    assert diarization.turns[0].start_seconds == 10
    assert requests[0].headers["idempotency-key"] == "t"
    assert b"whisper-1" in await requests[0].aread()
    assert b"gpt-4o-transcribe-diarize" in await requests[1].aread()


def test_openai_adapter_rejects_unsupported_timestamp_model() -> None:
    with pytest.raises(ValueError, match="timestamp-preserving"):
        OpenAITranscriptionAdapter(api_key="test-key", transcription_model="gpt-4o-transcribe")
