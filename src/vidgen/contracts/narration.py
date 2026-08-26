"""Versioned, provider-neutral contracts for T12 narration."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, model_validator

from .common import Score, StrictContract


class VoiceProfile(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    voice_profile_id: UUID
    project_id: UUID | None = None
    account_scope: str | None = None
    provider: str
    provider_voice_id: str
    model: str
    language: str = "en"
    default_speaking_instructions: str = ""
    default_pace: float = Field(default=1, ge=0.5, le=2)
    pronunciation_dictionary: dict[str, str] = Field(default_factory=dict)
    output_format: str = "wav"
    sample_rate_hz: int = Field(default=48000, gt=0)
    channels: Literal[1, 2] = 1
    profile_version: int = Field(gt=0)
    configuration_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    created_at: datetime
    updated_at: datetime


class NarrationProviderRequest(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    idempotency_key: str
    project_id: UUID
    script_id: UUID
    script_version: int = Field(gt=0)
    script_segment_id: UUID
    segment_sequence: int = Field(ge=0)
    text: str = Field(min_length=1)
    voice_profile_id: UUID
    voice_profile_version: int = Field(gt=0)
    voice_id: str
    model: str
    speaking_instructions: str = ""
    pronunciation_instructions: str = ""
    speed: float = Field(default=1, ge=0.5, le=2)
    output_format: str
    language: str
    provider_options: dict[str, str | int | float | bool] = Field(default_factory=dict)
    trace_context: dict[str, str] = Field(default_factory=dict)
    attempt_number: int = Field(ge=1, le=3)


class NarrationProviderResult(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    provider: str
    model: str
    provider_request_id: str
    attempt_number: int = Field(ge=1, le=3)
    content_type: str
    audio_format: str
    byte_size: int = Field(ge=0)
    usage: dict[str, int | float | str] = Field(default_factory=dict)
    response_metadata: dict[str, str | int | float | bool] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    provider_duration_seconds: float = Field(ge=0)
    idempotency_key: str


class NarrationWordTiming(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    word_index: int = Field(ge=0)
    word: str
    comparison_token: str
    punctuation: str = ""
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)
    confidence: Score

    @model_validator(mode="after")
    def valid_range(self) -> NarrationWordTiming:
        if self.end_seconds <= self.start_seconds:
            raise ValueError("word end time must be after start time")
        return self


class NarrationAlignment(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    timings: list[NarrationWordTiming]
    coverage: Score
    insertions: list[str] = Field(default_factory=list)
    omissions: list[str] = Field(default_factory=list)
    substitutions: list[str] = Field(default_factory=list)
    diagnostics: list[str] = Field(default_factory=list)


class NarrationQualityDiagnostic(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    code: str
    severity: Literal["warning", "error"]
    message: str
    measured_value: float | None = None
    threshold: float | None = None


class NarrationQualityReport(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    valid: bool
    diagnostics: list[NarrationQualityDiagnostic] = Field(default_factory=list)
    clipping_ratio: float = Field(ge=0, le=1)
    leading_silence_seconds: float = Field(ge=0)
    trailing_silence_seconds: float = Field(ge=0)
    speaking_rate_wpm: float = Field(ge=0)
    alignment_coverage: Score


class NarrationAttempt(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    attempt_id: UUID
    attempt_number: int = Field(ge=1, le=3)
    provider_result: NarrationProviderResult
    original_asset_id: UUID | None = None
    normalized_asset_id: UUID | None = None
    quality_report: NarrationQualityReport | None = None
    failure_classification: str | None = None


class NarrationSegmentResult(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    script_segment_id: UUID
    sequence: int = Field(ge=0)
    generation_identity: str = Field(pattern=r"^[a-f0-9]{64}$")
    normalized_asset_id: UUID
    duration_seconds: float = Field(gt=0)
    audio_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    alignment: NarrationAlignment
    quality_report: NarrationQualityReport
    selected_attempt_id: UUID


class NarrationPreviewManifest(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    script_id: UUID
    script_version: int = Field(gt=0)
    narration_run_id: UUID
    voice_profile_id: UUID
    voice_profile_version: int = Field(gt=0)
    segment_ids: list[UUID]
    narration_asset_ids: list[UUID]
    segment_durations_seconds: list[float]
    word_timing_references: list[UUID]
    concatenation_parameters: dict[str, Any]
    preview_duration_seconds: float = Field(ge=0)
    preview_asset_id: UUID
    input_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    output_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    warnings: list[str] = Field(default_factory=list)
    provenance: dict[str, Any]


class NarrationResult(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    narration_run_id: UUID
    project_id: UUID
    status: Literal["narration_complete", "narration_failed"]
    segments: list[NarrationSegmentResult]
    preview_manifest_asset_id: UUID | None = None
    preview: NarrationPreviewManifest | None = None
