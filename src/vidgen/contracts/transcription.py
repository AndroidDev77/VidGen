from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from vidgen.contracts.common import NonNegativeSeconds, PositiveSeconds, StrictContract


class TranscriptionWarning(StrictContract):
    code: str
    message: str
    chunk_sequence: int | None = Field(default=None, ge=0)


class TimeInterval(StrictContract):
    start_seconds: NonNegativeSeconds
    end_seconds: PositiveSeconds

    @model_validator(mode="after")
    def end_follows_start(self) -> TimeInterval:
        if self.end_seconds <= self.start_seconds:
            raise ValueError("interval end must follow start")
        return self


class AudioChunk(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    asset_id: UUID
    parent_audio_asset_id: UUID
    sequence: int = Field(ge=0)
    start_seconds: NonNegativeSeconds
    end_seconds: PositiveSeconds
    overlap_before_seconds: NonNegativeSeconds = 0
    overlap_after_seconds: NonNegativeSeconds = 0
    byte_size: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    codec: str
    sample_rate: int = Field(gt=0)
    idempotency_key: str

    @model_validator(mode="after")
    def validate_interval(self) -> AudioChunk:
        if self.end_seconds <= self.start_seconds:
            raise ValueError("chunk end must follow start")
        return self


class TranscriptWord(StrictContract):
    text: str = Field(min_length=1)
    start_seconds: NonNegativeSeconds
    end_seconds: PositiveSeconds
    confidence: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def validate_interval(self) -> TranscriptWord:
        if self.end_seconds <= self.start_seconds:
            raise ValueError("word end must follow start")
        return self


class TranscriptSegment(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    sequence: int = Field(ge=0)
    start_seconds: NonNegativeSeconds
    end_seconds: PositiveSeconds
    text: str = Field(min_length=1)
    speaker_label: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    source_chunk_ids: list[UUID] = Field(min_length=1)
    words: list[TranscriptWord] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_interval(self) -> TranscriptSegment:
        if self.end_seconds <= self.start_seconds:
            raise ValueError("segment end must follow start")
        if any(
            word.start_seconds < self.start_seconds or word.end_seconds > self.end_seconds
            for word in self.words
        ):
            raise ValueError("segment words must fall inside the segment interval")
        return self


class SpeakerTurn(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    sequence: int = Field(ge=0)
    speaker_label: str = Field(pattern=r"^speaker_[0-9]{3}$")
    start_seconds: NonNegativeSeconds
    end_seconds: PositiveSeconds
    confidence: float | None = Field(default=None, ge=0, le=1)
    source_chunk_ids: list[UUID] = Field(min_length=1)
    provider: str
    model: str
    alternate_labels: list[str] = Field(default_factory=list)
    warnings: list[TranscriptionWarning] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_interval(self) -> SpeakerTurn:
        if self.end_seconds <= self.start_seconds:
            raise ValueError("speaker turn end must follow start")
        return self


class ChunkTranscriptionResult(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    chunk: AudioChunk
    provider: str
    model: str
    provider_request_id: str
    attempt: int = Field(ge=1)
    language: str | None = None
    text: str
    segments: list[TranscriptSegment]
    words: list[TranscriptWord]
    confidence: float | None = Field(default=None, ge=0, le=1)
    raw_metadata: dict[str, object] = Field(default_factory=dict)
    warnings: list[TranscriptionWarning] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_order(self) -> ChunkTranscriptionResult:
        if [item.sequence for item in self.segments] != list(range(len(self.segments))):
            raise ValueError("chunk segment sequences must be contiguous and ordered")
        _validate_timed_order(self.words, "chunk words")
        return self


class DiarizationResult(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    provider: str
    model: str
    provider_request_ids: list[str]
    turns: list[SpeakerTurn]
    warnings: list[TranscriptionWarning] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_order(self) -> DiarizationResult:
        if [item.sequence for item in self.turns] != list(range(len(self.turns))):
            raise ValueError("speaker turn sequences must be contiguous and ordered")
        return self


class TranscriptCoverage(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    voiced_seconds: NonNegativeSeconds
    covered_voiced_seconds: NonNegativeSeconds
    ratio: float = Field(ge=0, le=1)
    passed: bool
    uncovered_intervals: list[TimeInterval] = Field(default_factory=list)


class TranscriptionRequest(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    idempotency_key: str
    chunk: AudioChunk
    language_hint: str | None = None
    context_prompt: str | None = None
    timestamp_granularity: Literal["word", "segment"] = "word"
    options: dict[str, object] = Field(default_factory=dict)


class DiarizationRequest(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    idempotency_key: str
    chunk: AudioChunk
    language_hint: str | None = None
    context_prompt: str | None = None
    options: dict[str, object] = Field(default_factory=dict)


class TranscriptionResult(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    project_id: UUID
    run_id: UUID
    transcript_id: UUID
    source_video_id: UUID
    source_audio_asset_id: UUID
    transcript_asset_id: UUID
    status: Literal["transcribed"]
    language: str | None = None
    text: str
    segments: list[TranscriptSegment]
    speaker_turns: list[SpeakerTurn]
    coverage: TranscriptCoverage
    warnings: list[TranscriptionWarning] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_order(self) -> TranscriptionResult:
        if [item.sequence for item in self.segments] != list(range(len(self.segments))):
            raise ValueError("transcript segment sequences must be contiguous and ordered")
        if [item.sequence for item in self.speaker_turns] != list(range(len(self.speaker_turns))):
            raise ValueError("speaker turn sequences must be contiguous and ordered")
        return self


def _validate_timed_order(items: list[TranscriptWord], label: str) -> None:
    starts = [item.start_seconds for item in items]
    if starts != sorted(starts):
        raise ValueError(f"{label} must be ordered by source timestamp")
