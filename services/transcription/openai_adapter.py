from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from vidgen.contracts.transcription import (
    ChunkTranscriptionResult,
    DiarizationRequest,
    DiarizationResult,
    SpeakerTurn,
    TranscriptionRequest,
    TranscriptionWarning,
    TranscriptSegment,
    TranscriptWord,
)


class OpenAITranscriptionAdapter:
    provider_name = "openai"
    _TIMESTAMP_MODELS = frozenset({"whisper-1"})
    _DIARIZATION_MODELS = frozenset({"gpt-4o-transcribe-diarize"})

    def __init__(
        self,
        *,
        api_key: str,
        transcription_model: str = "whisper-1",
        diarization_model: str = "gpt-4o-transcribe-diarize",
        base_url: str = "https://api.openai.com/v1",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("OpenAI API key is required")
        if transcription_model not in self._TIMESTAMP_MODELS:
            raise ValueError(
                "timestamp-preserving transcription requires a configured OpenAI "
                f"timestamp model; supported models: {sorted(self._TIMESTAMP_MODELS)}"
            )
        if diarization_model not in self._DIARIZATION_MODELS:
            raise ValueError(
                "unsupported OpenAI diarization model; supported models: "
                f"{sorted(self._DIARIZATION_MODELS)}"
            )
        self.transcription_model = transcription_model
        self.diarization_model = diarization_model
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(base_url=base_url, timeout=300)
        self.headers = {"Authorization": f"Bearer {api_key}"}

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def transcribe(
        self, request: TranscriptionRequest, audio_path: Path
    ) -> ChunkTranscriptionResult:
        data = {
            "model": self.transcription_model,
            "response_format": "verbose_json",
            "timestamp_granularities[]": request.timestamp_granularity,
        }
        if request.language_hint:
            data["language"] = request.language_hint
        if request.context_prompt:
            data["prompt"] = request.context_prompt
        response = await self._post(audio_path, data, request.idempotency_key)
        payload = _json_object(response)
        words = _parse_words(payload, request.chunk.start_seconds)
        provider_returned_words = bool(words)
        segments = _parse_segments(payload, request.chunk.asset_id, request.chunk.start_seconds)
        if not words:
            words = [
                TranscriptWord(
                    text=segment.text,
                    start_seconds=segment.start_seconds,
                    end_seconds=segment.end_seconds,
                    confidence=segment.confidence,
                )
                for segment in segments
            ]
        request_id = response.headers.get("x-request-id") or _stable_response_id(payload)
        return ChunkTranscriptionResult(
            chunk=request.chunk,
            provider=self.provider_name,
            model=self.transcription_model,
            provider_request_id=request_id,
            attempt=_attempt(request.options.get("attempt")),
            language=_optional_string(payload.get("language")) or request.language_hint,
            text=str(payload.get("text", "")),
            segments=segments,
            words=words,
            confidence=None,
            raw_metadata={"duration": payload.get("duration"), "response_format": "verbose_json"},
            warnings=[]
            if provider_returned_words
            else [
                TranscriptionWarning(
                    code="missing_words", message="provider returned no timed words"
                )
            ],
        )

    async def diarize(self, request: DiarizationRequest, audio_path: Path) -> DiarizationResult:
        data = {
            "model": self.diarization_model,
            "response_format": "diarized_json",
            "chunking_strategy": "auto",
        }
        if request.language_hint:
            data["language"] = request.language_hint
        response = await self._post(audio_path, data, request.idempotency_key)
        payload = _json_object(response)
        request_id = response.headers.get("x-request-id") or _stable_response_id(payload)
        labels: dict[str, str] = {}
        turns: list[SpeakerTurn] = []
        for raw in payload.get("segments", []):
            if not isinstance(raw, dict):
                continue
            provider_label = str(raw.get("speaker", "unknown"))
            labels.setdefault(provider_label, f"speaker_{len(labels) + 1:03d}")
            turns.append(
                SpeakerTurn(
                    sequence=len(turns),
                    speaker_label=labels[provider_label],
                    start_seconds=request.chunk.start_seconds + float(raw["start"]),
                    end_seconds=request.chunk.start_seconds + float(raw["end"]),
                    confidence=_optional_float(raw.get("confidence")),
                    source_chunk_ids=[request.chunk.asset_id],
                    provider=self.provider_name,
                    model=self.diarization_model,
                    alternate_labels=[],
                    warnings=[],
                )
            )
        return DiarizationResult(
            provider=self.provider_name,
            model=self.diarization_model,
            provider_request_ids=[request_id],
            turns=turns,
            warnings=[],
        )

    async def _post(
        self, audio_path: Path, data: dict[str, str], idempotency_key: str
    ) -> httpx.Response:
        with audio_path.open("rb") as stream:
            response = await self.client.post(
                "/audio/transcriptions",
                headers={**self.headers, "Idempotency-Key": idempotency_key},
                data=data,
                files={"file": (audio_path.name, stream, "audio/flac")},
            )
        response.raise_for_status()
        return response


def _json_object(response: httpx.Response) -> dict[str, Any]:
    value = response.json()
    if not isinstance(value, dict):
        raise ValueError("transcription provider returned a non-object response")
    return value


def _parse_words(payload: dict[str, Any], offset: float) -> list[TranscriptWord]:
    result: list[TranscriptWord] = []
    for raw in payload.get("words", []):
        if isinstance(raw, dict) and raw.get("word") and "start" in raw and "end" in raw:
            result.append(
                TranscriptWord(
                    text=str(raw["word"]),
                    start_seconds=offset + float(raw["start"]),
                    end_seconds=offset + float(raw["end"]),
                    confidence=_optional_float(raw.get("confidence")),
                )
            )
    return result


def _parse_segments(
    payload: dict[str, Any], chunk_id: Any, offset: float
) -> list[TranscriptSegment]:
    result: list[TranscriptSegment] = []
    for raw in payload.get("segments", []):
        if isinstance(raw, dict) and raw.get("text") and "start" in raw and "end" in raw:
            result.append(
                TranscriptSegment(
                    sequence=len(result),
                    start_seconds=offset + float(raw["start"]),
                    end_seconds=offset + float(raw["end"]),
                    text=str(raw["text"]).strip(),
                    confidence=_optional_float(raw.get("confidence")),
                    source_chunk_ids=[chunk_id],
                    words=[],
                )
            )
    return result


def _optional_float(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) else None


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _stable_response_id(payload: dict[str, Any]) -> str:
    import hashlib

    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:24]
    return f"openai_response_{digest}"


def _attempt(value: object) -> int:
    return value if isinstance(value, int) else 1
