from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from vidgen.contracts.common import NonNegativeSeconds, PositiveSeconds, StrictContract


class EvidenceDiagnostic(StrictContract):
    code: str
    severity: Literal["warning", "error"]
    message: str
    scene_sequence: int | None = Field(default=None, ge=0)


class SourceTimeRange(StrictContract):
    start_seconds: NonNegativeSeconds
    end_seconds: PositiveSeconds

    @model_validator(mode="after")
    def ordered(self) -> SourceTimeRange:
        if self.end_seconds <= self.start_seconds:
            raise ValueError("source time range end must follow start")
        return self


class EvidenceTranscriptItem(StrictContract):
    source_range: SourceTimeRange
    source_asset_id: UUID
    text: str = Field(min_length=1)
    speaker_label: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    segment_sequence: int = Field(ge=0)


class SceneEvidence(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    scene_sequence: int = Field(ge=0)
    source_range: SourceTimeRange
    source_video_asset_id: UUID
    source_audio_asset_id: UUID | None = None
    representative_frame_asset_ids: list[UUID] = Field(min_length=1)
    representative_frame_timestamps: list[NonNegativeSeconds] = Field(min_length=1)
    transcript_items: list[EvidenceTranscriptItem] = Field(default_factory=list)

    @model_validator(mode="after")
    def matching_frames(self) -> SceneEvidence:
        if len(self.representative_frame_asset_ids) != len(self.representative_frame_timestamps):
            raise ValueError("frame IDs and timestamps must have equal length")
        if any(
            timestamp < self.source_range.start_seconds or timestamp > self.source_range.end_seconds
            for timestamp in self.representative_frame_timestamps
        ):
            raise ValueError("representative frames must fall inside the scene")
        return self


class EvidenceProvenance(StrictContract):
    transcript_origin: Literal["subtitle", "audio_transcription"]
    transcript_id: UUID
    transcript_asset_id: UUID
    subtitle_asset_id: UUID | None = None
    input_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    builder_version: str
    generation_parameters: dict[str, object] = Field(default_factory=dict)


class EvidencePackage(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    package_id: UUID
    project_id: UUID
    version: int = Field(ge=1)
    source_video_id: UUID
    source_video_asset_id: UUID
    contact_sheet_asset_id: UUID | None = None
    scenes: list[SceneEvidence]
    provenance: EvidenceProvenance
    diagnostics: list[EvidenceDiagnostic] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_scenes(self) -> EvidencePackage:
        sequences = [scene.scene_sequence for scene in self.scenes]
        if len(sequences) != len(set(sequences)):
            raise ValueError("scene sequences must be unique")
        return self
