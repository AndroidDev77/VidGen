"""Strict, provider-neutral T15 animation contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, field_validator
from pydantic.json_schema import SkipJsonSchema

from vidgen.contracts.common import StrictContract


class VideoProvider(StrEnum):
    RUNWAY = "runway"
    FAKE = "fake"


class RunwayModel(StrEnum):
    GEN4_TURBO = "gen4_turbo"
    GEN4_5 = "gen4.5"


class VideoTaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class VideoFormat(StrEnum):
    MP4 = "mp4"


class VideoCodec(StrEnum):
    H264 = "h264"
    HEVC = "hevc"


class VideoContainer(StrEnum):
    MP4 = "mp4"


class MotionIntent(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    shot_id: UUID
    shot_sequence: int = Field(ge=0)
    visual_purpose: str
    primary_action: str = Field(min_length=1)
    start_pose: str
    expected_end_pose: str
    camera_movement: str
    motion_intensity: str
    subject_priority: list[str] = Field(default_factory=list)
    character_state: list[str] = Field(default_factory=list)
    prop_state: list[str] = Field(default_factory=list)
    environment_motion: list[str] = Field(default_factory=list)
    timing_beats: list[str] = Field(default_factory=list)
    continuity_invariants: list[str] = Field(default_factory=list)
    negative_motion_constraints: list[str] = Field(default_factory=list)


class MotionPromptPackage(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    intent: MotionIntent
    compiler_version: str
    template_version: str
    prompt: str
    prompt_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    diagnostics: list[str] = Field(default_factory=list)
    provider_parameters: dict[str, Any] = Field(default_factory=dict)


class VideoProviderRequest(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    application_idempotency_key: str = Field(min_length=1, max_length=255)
    project_id: UUID
    animation_run_id: UUID
    animation_item_id: UUID
    storyboard_id: UUID
    storyboard_version: int = Field(gt=0)
    shot_id: UUID
    shot_sequence: int = Field(ge=0)
    first_keyframe_asset_id: UUID
    first_keyframe_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    last_keyframe_asset_id: UUID | None = None
    last_keyframe_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    compiled_motion_prompt: str = Field(min_length=1)
    provider: VideoProvider
    model: RunwayModel
    requested_duration_seconds: float = Field(gt=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    output_format: VideoFormat = VideoFormat.MP4
    seed: int | None = Field(default=None, ge=0)
    provider_options: dict[str, Any] = Field(default_factory=dict)
    trace_context: dict[str, str] = Field(default_factory=dict)
    attempt_number: int = Field(gt=0)
    provider_configuration_version: str


class VideoProviderTask(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    provider: VideoProvider
    model: RunwayModel
    remote_task_id: str
    requested_at: datetime
    status: VideoTaskStatus
    provider_request_id: str | None = None
    attempt_number: int = Field(gt=0)
    requested_duration_seconds: float = Field(gt=0)
    progress: float | None = Field(default=None, ge=0, le=1)
    usage: dict[str, float] = Field(default_factory=dict)
    failure_reason: str | None = None
    provider_error_code: str | None = None
    metadata: dict[str, str | int | float | bool] = Field(default_factory=dict)
    completed_at: datetime | None = None
    last_polled_at: datetime | None = None
    latency_ms: int | None = Field(default=None, ge=0)
    application_idempotency_key: str
    provider_configuration_version: str
    # Transport handles expire and must never be serialized into durable state.
    output_handles: SkipJsonSchema[tuple[str, ...]] = Field(default=(), exclude=True, repr=False)

    @field_validator("requested_at", "completed_at", "last_polled_at")
    @classmethod
    def timestamps_are_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("provider timestamps must be timezone-aware UTC instants")
        return value.astimezone(UTC)


class VideoProviderResult(VideoProviderTask):
    output_count: int = Field(default=0, ge=0)


class VideoProbeResult(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    container: VideoContainer
    video_codec: VideoCodec
    audio_codec: str | None = None
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    display_aspect_ratio: str
    pixel_format: str
    frame_rate: str
    timebase: str
    duration_seconds: float = Field(gt=0)
    frame_count: int | None = Field(default=None, gt=0)
    byte_size: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    ffprobe_json: dict[str, Any]
    ffprobe_version: str


class VideoValidationDiagnostic(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    code: str
    severity: Literal["error", "warning"]
    message: str


class VideoValidationReport(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    valid: bool
    probe: VideoProbeResult | None = None
    diagnostics: list[VideoValidationDiagnostic] = Field(default_factory=list)


class VideoTrimManifest(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    trim_in_seconds: float = Field(ge=0)
    trim_out_seconds: float = Field(ge=0)
    usable_duration_seconds: float = Field(gt=0)
    ffmpeg_arguments: list[str] = Field(default_factory=list)
    encoding_profile: str


class GeneratedVideoCandidate(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    generated_video_id: UUID
    original_asset_id: UUID
    canonical_asset_id: UUID
    shot_id: UUID
    selected: bool
    validation: VideoValidationReport


class ShotAnimationResult(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    shot_id: UUID
    status: Literal["completed", "reused", "polling", "failed"]
    remote_task_id: str | None = None
    candidate: GeneratedVideoCandidate | None = None
    error_code: str | None = None


class AnimationRunRequest(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    project_id: UUID
    storyboard_id: UUID | None = None
    image_generation_run_id: UUID | None = None
    idempotency_key: str
    provider_configuration_version: str
    provider: VideoProvider = VideoProvider.FAKE
    model: RunwayModel | None = None
    shot_id: UUID | None = None


class AnimationRunResult(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    run_id: UUID
    storyboard_id: UUID
    image_generation_run_id: UUID
    requested_count: int = Field(ge=0)
    submitted_count: int = Field(ge=0)
    polling_count: int = Field(ge=0)
    completed_count: int = Field(ge=0)
    reused_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    status: str


class AnimationResult(AnimationRunResult):
    items: list[ShotAnimationResult] = Field(default_factory=list)
