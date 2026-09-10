"""Versioned, provider-neutral contracts for T12 narration."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from .common import Score, StrictContract

#: Every deterministic quality code the T12 gate can emit, and therefore every
#: code a deployment or project may choose to demote to a warning. A warning
#: is still recorded on the attempt's quality report; it just no longer makes
#: the segment fail and pay for another provider attempt.
NARRATION_QUALITY_CODES: tuple[str, ...] = (
    "clipping",
    "leading_silence",
    "trailing_silence",
    "internal_silence",
    "speaking_rate",
    "alignment_coverage",
)
NARRATION_WARN_ONLY_ELIGIBLE_QUALITY_CODES: tuple[str, ...] = NARRATION_QUALITY_CODES
#: alignment_coverage by default: the coverage figure is how much of the
#: approved text Whisper transcribed back verbatim, and Whisper is unreliable
#: on names and domain vocabulary even when the audio is perfectly clear. A low
#: figure is worth surfacing but is not worth discarding good audio and paying
#: to regenerate it.
DEFAULT_NARRATION_WARN_ONLY_QUALITY_CODES: tuple[str, ...] = ("alignment_coverage",)


def _known_quality_codes(value: list[str]) -> list[str]:
    unknown = sorted(set(value) - set(NARRATION_WARN_ONLY_ELIGIBLE_QUALITY_CODES))
    if unknown:
        raise ValueError(
            f"unknown narration quality codes: {', '.join(unknown)}; "
            f"expected any of {', '.join(NARRATION_WARN_ONLY_ELIGIBLE_QUALITY_CODES)}"
        )
    # Deterministic and duplicate-free: the list is bound into the narration
    # generation identity and compared to decide whether settings changed.
    return sorted(set(value))


class NarrationQualityThresholds(StrictContract):
    """The fully resolved T12 quality gate: every limit and the warn-only set.

    The pipeline binds these values into each segment's generation identity, so
    changing any of them mints new identities rather than silently accepting
    or rejecting audio that was judged under different rules.
    """

    schema_version: Literal["1.0"] = "1.0"
    min_wpm: float = Field(default=80, gt=0)
    max_wpm: float = Field(default=220, gt=0)
    #: 0.75 rather than the original 0.90: the figure is how much of the
    #: approved text Whisper transcribed back verbatim, and clear audio over
    #: domain vocabulary routinely scores 75-85%. Coverage is warn-only by
    #: default as well; the lower floor is the safety net for a project that
    #: makes it fail again.
    min_alignment_coverage: float = Field(default=0.75, ge=0, le=1)
    max_clipping_ratio: float = Field(default=0.001, ge=0, le=1)
    max_leading_silence: float = Field(default=0.5, ge=0)
    max_trailing_silence: float = Field(default=0.7, ge=0)
    max_internal_silence: float = Field(default=1.5, ge=0)
    #: Quality codes recorded as warnings instead of failing the attempt.
    warn_only_codes: list[str] = Field(
        default_factory=lambda: list(DEFAULT_NARRATION_WARN_ONLY_QUALITY_CODES), max_length=16
    )

    @field_validator("warn_only_codes")
    @classmethod
    def validate_warn_only_codes(cls, value: list[str]) -> list[str]:
        return _known_quality_codes(value)

    @model_validator(mode="after")
    def speaking_rate_window_is_ordered(self) -> NarrationQualityThresholds:
        if self.min_wpm >= self.max_wpm:
            raise ValueError("min_wpm must be below max_wpm")
        return self


class NarrationQualityThresholdOverrides(StrictContract):
    """A project's partial override of the deployment's quality thresholds.

    Every limit is optional: ``None`` means "use the deployment default" for
    that one limit, so a project may relax alignment coverage alone without
    restating every other number. The warn-only set is overridden separately
    on the generation settings, mirroring the validation-code overrides.
    """

    schema_version: Literal["1.0"] = "1.0"
    min_wpm: float | None = Field(default=None, gt=0)
    max_wpm: float | None = Field(default=None, gt=0)
    min_alignment_coverage: float | None = Field(default=None, ge=0, le=1)
    max_clipping_ratio: float | None = Field(default=None, ge=0, le=1)
    max_leading_silence: float | None = Field(default=None, ge=0)
    max_trailing_silence: float | None = Field(default=None, ge=0)
    max_internal_silence: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def speaking_rate_window_is_ordered(self) -> NarrationQualityThresholdOverrides:
        if self.min_wpm is not None and self.max_wpm is not None and self.min_wpm >= self.max_wpm:
            raise ValueError("min_wpm must be below max_wpm")
        return self

    def apply(self, defaults: NarrationQualityThresholds) -> NarrationQualityThresholds:
        """The deployment thresholds with every set override applied on top."""
        values = defaults.model_dump(exclude={"schema_version"})
        values.update(
            {
                key: value
                for key, value in self.model_dump(exclude={"schema_version"}).items()
                if value is not None
            }
        )
        return NarrationQualityThresholds.model_validate(values)


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
    selected_attempt_id: UUID | None = None
    reused_from_segment_id: UUID | None = None


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
