"""Strict public contracts for T17 deterministic captioning and rendering."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import Field, model_validator

from vidgen.contracts.common import StrictContract

SHA256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Micros = Annotated[int, Field(ge=0)]


class TransitionKind(StrEnum):
    CUT = "cut"
    CROSSFADE = "crossfade"


class RenderInputReference(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    asset_id: UUID
    sha256: SHA256
    media_type: str = Field(min_length=1, max_length=128)
    role: str = Field(min_length=1, max_length=64)


class RenderTransition(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    kind: TransitionKind = TransitionKind.CUT
    duration_us: Micros = 0
    handle_in_us: Micros = 0
    handle_out_us: Micros = 0

    @model_validator(mode="after")
    def valid_cut(self) -> RenderTransition:
        if self.kind == TransitionKind.CUT and any(
            (self.duration_us, self.handle_in_us, self.handle_out_us)
        ):
            raise ValueError("hard cuts cannot carry duration or handles")
        if self.kind == TransitionKind.CROSSFADE and self.duration_us <= 0:
            raise ValueError("crossfade duration must be positive")
        return self


class RenderShotEntry(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    shot_id: UUID
    sequence: int = Field(ge=0)
    shot_workflow_identity: SHA256
    animation_run_id: UUID
    video: RenderInputReference
    source_width: int = Field(gt=0, le=16384)
    source_height: int = Field(gt=0, le=16384)
    source_frame_rate: str = Field(pattern=r"^[1-9][0-9]*/[1-9][0-9]*$")
    source_codec: str = Field(min_length=1, max_length=32)
    measured_source_duration_us: int = Field(gt=0)
    global_start_us: Micros
    global_end_us: int = Field(gt=0)
    exact_usable_duration_us: int = Field(gt=0)
    trim_start_us: Micros = 0
    trim_end_us: int = Field(gt=0)
    transition_in: RenderTransition = Field(default_factory=RenderTransition)
    transition_out: RenderTransition = Field(default_factory=RenderTransition)
    normalization_policy: Literal["scale_crop", "scale_pad"] = "scale_pad"
    warning_codes: list[str] = Field(default_factory=list, max_length=32)
    parent_asset_ids: list[UUID] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def exact_ranges(self) -> RenderShotEntry:
        if self.global_end_us <= self.global_start_us:
            raise ValueError("global end must follow start")
        if self.trim_end_us <= self.trim_start_us:
            raise ValueError("trim end must follow start")
        if self.global_end_us - self.global_start_us != self.exact_usable_duration_us:
            raise ValueError("global range must equal exact usable duration")
        if self.trim_end_us - self.trim_start_us != self.exact_usable_duration_us:
            raise ValueError("trim range must equal exact usable duration")
        return self


class RenderAudioEntry(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    role: Literal["narration", "dialogue", "sfx", "music"]
    asset: RenderInputReference
    start_us: Micros = 0
    duration_us: int = Field(gt=0)
    gain_millidb: int = Field(default=0, ge=-60000, le=12000)
    duck_under_narration: bool = False


class RenderVideoProfile(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    width: Literal[1920] = 1920
    height: Literal[1080] = 1080
    frame_rate: Literal[24, 30] = 24
    codec: Literal["libx264"] = "libx264"
    codec_profile: Literal["high"] = "high"
    pixel_format: Literal["yuv420p"] = "yuv420p"
    normalization_policy: Literal["scale_crop", "scale_pad"] = "scale_pad"


class RenderAudioProfile(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    codec: Literal["aac"] = "aac"
    sample_rate_hz: Literal[48000] = 48000
    channels: Literal[2] = 2
    bitrate_kbps: int = Field(default=320, ge=96, le=512)
    integrated_lufs: int = Field(default=-14, ge=-24, le=-5)
    true_peak_dbtp: float = Field(default=-1.5, ge=-3, le=-0.1)
    max_lra: float = Field(default=11, ge=1, le=20)


class CaptionWord(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    sequence: int = Field(ge=0)
    text: str = Field(min_length=1, max_length=128)
    start_us: Micros
    end_us: int = Field(gt=0)

    @model_validator(mode="after")
    def ordered(self) -> CaptionWord:
        if self.end_us <= self.start_us:
            raise ValueError("word end must follow start")
        return self


class CaptionCue(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    sequence: int = Field(ge=1)
    start_us: Micros
    end_us: int = Field(gt=0)
    lines: list[str] = Field(min_length=1, max_length=2)
    word_start: int = Field(ge=0)
    word_end: int = Field(gt=0)

    @model_validator(mode="after")
    def ordered(self) -> CaptionCue:
        if self.end_us <= self.start_us:
            raise ValueError("cue end must follow start")
        if any(not line.strip() for line in self.lines):
            raise ValueError("caption lines cannot be empty")
        return self


class CaptionTrack(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    caption_track_id: UUID
    language: str = Field(pattern=r"^[a-z]{2,3}(-[A-Z]{2})?$")
    cues: list[CaptionCue] = Field(min_length=1, max_length=10000)
    duration_us: int = Field(gt=0)
    safe_zone_percent: int = Field(default=10, ge=0, le=30)
    pipeline_version: Literal["captions/1"] = "captions/1"

    @model_validator(mode="after")
    def dense_nonoverlapping(self) -> CaptionTrack:
        for i, cue in enumerate(self.cues):
            if cue.sequence != i + 1:
                raise ValueError("caption cue sequences must be dense")
            if cue.end_us > self.duration_us:
                raise ValueError("caption cue exceeds narration duration")
            if i and cue.start_us < self.cues[i - 1].end_us:
                raise ValueError("caption cues overlap")
        return self


class CaptionValidationDiagnostic(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    code: str = Field(min_length=1, max_length=64)
    severity: Literal["warning", "error"]
    message: str = Field(min_length=1, max_length=512)
    cue_sequence: int | None = Field(default=None, ge=1)


class CaptionValidationReport(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    valid: bool
    caption_identity: SHA256
    diagnostics: list[CaptionValidationDiagnostic] = Field(default_factory=list, max_length=1000)
    adjustment_codes: list[str] = Field(default_factory=list, max_length=1000)


class RenderManifest(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    manifest_id: UUID
    render_identity: SHA256
    project_id: UUID
    approved_script_id: UUID
    approved_script_version: int = Field(gt=0)
    approved_script_hash: SHA256
    narration_run_id: UUID
    narration_assets: list[RenderInputReference] = Field(min_length=1, max_length=1000)
    narration_word_timing_hash: SHA256
    narration_duration_us: int = Field(gt=0)
    storyboard_run_id: UUID
    storyboard_hash: SHA256
    timing_manifest_id: UUID
    timing_manifest_hash: SHA256
    t16_result_id: str = Field(min_length=1, max_length=255)
    shots: list[RenderShotEntry] = Field(min_length=1, max_length=500)
    caption_track_id: UUID
    caption_assets: list[RenderInputReference] = Field(min_length=2, max_length=3)
    audio_entries: list[RenderAudioEntry] = Field(min_length=1, max_length=128)
    video_profile: RenderVideoProfile = Field(default_factory=RenderVideoProfile)
    audio_profile: RenderAudioProfile = Field(default_factory=RenderAudioProfile)
    subtitle_mode: Literal["selectable", "burn_in", "both"] = "selectable"
    ffmpeg_profile_version: Literal["ffmpeg/1"] = "ffmpeg/1"
    verification_profile_version: Literal["verify/1"] = "verify/1"
    pipeline_version: Literal["t17/1"] = "t17/1"
    input_hash: SHA256
    idempotency_key: str = Field(min_length=1, max_length=255)
    created_at: datetime
    provenance: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def canonical_timeline(self) -> RenderManifest:
        for index, shot in enumerate(self.shots):
            if shot.sequence != index:
                raise ValueError("shot sequences must be dense")
            expected = 0 if index == 0 else self.shots[index - 1].global_end_us
            if shot.global_start_us != expected:
                raise ValueError("shot coverage contains a gap or overlap")
        if self.shots[-1].global_end_us != self.narration_duration_us:
            raise ValueError("visual duration must equal narration duration")
        if sum(entry.role == "narration" for entry in self.audio_entries) != 1:
            raise ValueError("exactly one narration entry is required")
        return self


class RenderCommandPlan(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    render_identity: SHA256
    normalization_arguments: list[list[str]]
    picture_arguments: list[str]
    premaster_arguments: list[str]
    loudness_pass1_arguments: list[str]
    loudness_pass2_arguments: list[str]
    final_arguments: list[str]
    command_plan_hash: SHA256


class RenderVerificationThresholds(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    duration_tolerance_us: int = Field(default=80000, ge=0, le=500000)
    loudness_tolerance_lu: float = Field(default=1.0, ge=0, le=3)
    true_peak_tolerance_db: float = Field(default=0.5, ge=0, le=2)


class RenderVerificationReport(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    render_job_id: UUID
    render_identity: SHA256
    manifest_asset_id: UUID
    manifest_hash: SHA256
    final_video_asset_id: UUID
    final_video_hash: SHA256
    caption_asset_ids: list[UUID] = Field(min_length=2, max_length=3)
    caption_hashes: list[SHA256] = Field(min_length=2, max_length=3)
    ffmpeg_version: str = Field(max_length=256)
    ffprobe_version: str = Field(max_length=256)
    encoder: str = Field(max_length=128)
    command_plan_hash: SHA256
    stream_metadata: dict[str, Any]
    measured_duration_us: int = Field(gt=0)
    expected_duration_us: int = Field(gt=0)
    duration_difference_us: int
    frame_count: int | None = Field(default=None, ge=0)
    frame_rate: str
    audio_sample_rate_hz: int = Field(gt=0)
    integrated_lufs: float
    true_peak_dbtp: float
    loudness_range: float
    black_intervals: list[dict[str, int]] = Field(default_factory=list)
    freeze_intervals: list[dict[str, int]] = Field(default_factory=list)
    silence_intervals: list[dict[str, int]] = Field(default_factory=list)
    full_decode_ok: bool
    subtitle_valid: bool
    verified: bool
    warning_codes: list[str] = Field(default_factory=list)
    failures: list[str] = Field(default_factory=list)
    verification_profile_version: Literal["verify/1"] = "verify/1"
    created_at: datetime
    provenance: dict[str, Any] = Field(default_factory=dict)


class RenderJobResult(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    render_job_id: UUID
    render_identity: SHA256
    status: str
    manifest_asset_id: UUID | None = None
    srt_asset_id: UUID | None = None
    webvtt_asset_id: UUID | None = None
    final_video_asset_id: UUID | None = None
    verification_report_asset_id: UUID | None = None
    reused: bool = False


class RenderFailure(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    classification: Literal["lineage", "validation", "transient", "cancelled", "execution"]
    code: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=1024)
    retryable: bool
    diagnostics: list[CaptionValidationDiagnostic] = Field(default_factory=list)
