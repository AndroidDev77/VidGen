from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from vidgen.contracts.common import NonNegativeSeconds, PositiveSeconds, StrictContract
from vidgen.contracts.transcription import (
    TranscriptCoverage,
    TranscriptionWarning,
    TranscriptSegment,
)


class SubtitleCue(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    sequence: int = Field(ge=0)
    start_seconds: NonNegativeSeconds
    end_seconds: PositiveSeconds
    text: str = Field(min_length=1)
    speaker_hint: str | None = None

    @model_validator(mode="after")
    def validate_interval(self) -> SubtitleCue:
        if self.end_seconds <= self.start_seconds:
            raise ValueError("subtitle cue end must follow start")
        return self


class SubtitleCandidate(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    candidate_id: str = Field(min_length=1)
    source_type: Literal["embedded", "sidecar", "provider"]
    provider: str
    provider_subtitle_id: str | None = None
    provider_file_id: int | None = None
    asset_id: UUID | None = None
    stream_index: int | None = Field(default=None, ge=0)
    language: str | None = None
    subtitle_format: str
    hearing_impaired: bool = False
    forced: bool = False
    release_name: str | None = None
    file_name: str | None = None
    fps: float | None = Field(default=None, gt=0)
    download_count: int = Field(default=0, ge=0)
    metadata: dict[str, object] = Field(default_factory=dict)


class SubtitleQuality(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    candidate_id: str
    score: float = Field(ge=0, le=1)
    cue_count: int = Field(ge=0)
    timeline_coverage: float = Field(ge=0, le=1)
    voiced_coverage: float | None = Field(default=None, ge=0, le=1)
    sync_offset_seconds: float | None = None
    sync_correlation: float | None = None
    passed: bool
    reasons: list[str] = Field(default_factory=list)


class SubtitleSearchRequest(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    idempotency_key: str
    movie_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{16}$")
    byte_size: int | None = Field(default=None, gt=0)
    query: str | None = None
    imdb_id: str | None = None
    season_number: int | None = Field(default=None, ge=0)
    episode_number: int | None = Field(default=None, ge=0)
    languages: list[str] = Field(min_length=1)


class ProviderSubtitleDownload(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    candidate_id: str
    provider: str
    provider_request_id: str
    file_name: str
    media_type: str
    content: bytes
    remaining_downloads: int | None = Field(default=None, ge=0)


class CanonicalSubtitleTranscriptArtifact(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    project_id: UUID
    subtitle_run_id: UUID
    transcript_id: UUID
    source_video_id: UUID
    source_subtitle_asset_id: UUID
    language: str | None = None
    text: str
    segments: list[TranscriptSegment]
    coverage: TranscriptCoverage
    candidate: SubtitleCandidate
    quality: SubtitleQuality
    warnings: list[TranscriptionWarning] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_order(self) -> CanonicalSubtitleTranscriptArtifact:
        if [item.sequence for item in self.segments] != list(range(len(self.segments))):
            raise ValueError("subtitle transcript segments must be contiguous and ordered")
        return self


class SubtitleImportResult(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    project_id: UUID
    subtitle_run_id: UUID
    transcript_id: UUID
    source_video_id: UUID
    source_subtitle_asset_id: UUID
    transcript_asset_id: UUID
    status: Literal["subtitle_imported"]
    language: str | None = None
    text: str
    segments: list[TranscriptSegment]
    coverage: TranscriptCoverage
    candidate: SubtitleCandidate
    quality: SubtitleQuality
    warnings: list[TranscriptionWarning] = Field(default_factory=list)
