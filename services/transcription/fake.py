from __future__ import annotations

import hashlib
from pathlib import Path

from vidgen.contracts.transcription import (
    ChunkTranscriptionResult,
    DiarizationRequest,
    DiarizationResult,
    SpeakerTurn,
    TranscriptionRequest,
    TranscriptSegment,
    TranscriptWord,
)


class FakeTranscriptionProvider:
    provider_name = "fake"
    transcription_model = "fake-transcribe-v1"
    diarization_model = "fake-diarize-v1"

    def __init__(self, *, fail_once_sequences: set[int] | None = None) -> None:
        self.fail_once_sequences = fail_once_sequences or set()
        self.transcription_calls: list[int] = []
        self.diarization_calls: list[int] = []
        self._failed: set[int] = set()

    async def transcribe(
        self, request: TranscriptionRequest, audio_path: Path
    ) -> ChunkTranscriptionResult:
        del audio_path
        sequence = request.chunk.sequence
        self.transcription_calls.append(sequence)
        if sequence in self.fail_once_sequences and sequence not in self._failed:
            self._failed.add(sequence)
            raise TimeoutError(f"fake timeout for chunk {sequence}")
        start = request.chunk.start_seconds
        end = request.chunk.end_seconds
        midpoint = (start + end) / 2
        words = [
            TranscriptWord(text=f"chunk{sequence}", start_seconds=start, end_seconds=midpoint),
            TranscriptWord(text="dialogue", start_seconds=midpoint, end_seconds=end),
        ]
        text = " ".join(word.text for word in words)
        return ChunkTranscriptionResult(
            chunk=request.chunk,
            provider=self.provider_name,
            model=self.transcription_model,
            provider_request_id=_request_id("transcribe", request.idempotency_key),
            attempt=_attempt(request.options.get("attempt")),
            language=request.language_hint or "en",
            text=text,
            segments=[
                TranscriptSegment(
                    sequence=0,
                    start_seconds=start,
                    end_seconds=end,
                    text=text,
                    confidence=1,
                    source_chunk_ids=[request.chunk.asset_id],
                    words=words,
                )
            ],
            words=words,
            confidence=1,
            raw_metadata={"fake": True},
            warnings=[],
        )

    async def diarize(self, request: DiarizationRequest, audio_path: Path) -> DiarizationResult:
        del audio_path
        sequence = request.chunk.sequence
        self.diarization_calls.append(sequence)
        return DiarizationResult(
            provider=self.provider_name,
            model=self.diarization_model,
            provider_request_ids=[_request_id("diarize", request.idempotency_key)],
            turns=[
                SpeakerTurn(
                    sequence=0,
                    speaker_label="speaker_001",
                    start_seconds=request.chunk.start_seconds,
                    end_seconds=request.chunk.end_seconds,
                    confidence=1,
                    source_chunk_ids=[request.chunk.asset_id],
                    provider=self.provider_name,
                    model=self.diarization_model,
                )
            ],
        )


def _request_id(kind: str, key: str) -> str:
    return f"fake_{kind}_{hashlib.sha256(key.encode()).hexdigest()[:24]}"


def _attempt(value: object) -> int:
    return value if isinstance(value, int) else 1
