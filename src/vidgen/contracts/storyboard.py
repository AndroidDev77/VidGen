"""Storyboard and shot contracts."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from vidgen.contracts.common import PositiveSeconds, StrictContract
from vidgen.contracts.episode import CharacterState


class ShotDefinition(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    shot_id: UUID
    segment_id: UUID
    sequence: int = Field(ge=0)
    duration_seconds: PositiveSeconds
    location_id: UUID
    character_states: list[CharacterState] = Field(default_factory=list)
    action: str = Field(min_length=1)
    composition: str = Field(min_length=1)
    camera_motion: str = "locked"
    visual_gag: str | None = None
    image_prompt: str = ""
    video_prompt: str = ""
    negative_prompt: str = "random text, watermark, extra limbs, duplicate characters"
    reference_asset_ids: list[UUID] = Field(default_factory=list)
    seed: int | None = Field(default=None, ge=0)
    max_provider_clip_seconds: PositiveSeconds = 10


class Storyboard(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    project_id: UUID
    script_revision: int = Field(ge=1)
    total_duration_seconds: PositiveSeconds
    visual_style: str = Field(min_length=1)
    shots: list[ShotDefinition] = Field(min_length=1)

    @model_validator(mode="after")
    def shot_sequences_are_unique(self) -> Storyboard:
        sequences = [shot.sequence for shot in self.shots]
        if len(sequences) != len(set(sequences)):
            raise ValueError("shot sequences must be unique")
        return self
