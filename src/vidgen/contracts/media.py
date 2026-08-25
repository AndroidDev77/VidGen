from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from vidgen.contracts.common import NonNegativeSeconds, PositiveSeconds, StrictContract


class VideoStreamInfo(StrictContract):
    codec: str
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    frame_rate: float = Field(gt=0)
    pixel_format: str | None = None


class AudioStreamInfo(StrictContract):
    codec: str
    sample_rate: int | None = Field(default=None, gt=0)
    channels: int | None = Field(default=None, gt=0)


class MediaProbeResult(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    duration_seconds: PositiveSeconds
    format_name: str
    byte_size: int = Field(ge=0)
    video: VideoStreamInfo
    audio_streams: list[AudioStreamInfo] = Field(default_factory=list)
    raw_probe: dict[str, object]


class AudioExtractionResult(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    asset_id: UUID
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    duration_seconds: PositiveSeconds
    sample_rate: int = Field(gt=0)
    channels: int = Field(gt=0)
    codec: str


class SceneBoundary(StrictContract):
    sequence: int = Field(ge=0)
    start_seconds: NonNegativeSeconds
    end_seconds: PositiveSeconds
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def end_follows_start(self) -> SceneBoundary:
        if self.end_seconds <= self.start_seconds:
            raise ValueError("scene end must follow start")
        return self


class SceneDetectionResult(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    threshold: float = Field(gt=0, lt=1)
    duration_seconds: PositiveSeconds
    scenes: list[SceneBoundary] = Field(min_length=1)


class ExtractedFrame(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    asset_id: UUID
    scene_sequence: int = Field(ge=0)
    timestamp_seconds: NonNegativeSeconds
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class MediaProcessingResult(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    project_id: UUID
    source_video_id: UUID
    source_asset_id: UUID
    probe: MediaProbeResult
    audio: AudioExtractionResult
    scene_detection: SceneDetectionResult
    frames: list[ExtractedFrame]
