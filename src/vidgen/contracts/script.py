"""Recap script contracts."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from vidgen.contracts.common import PositiveSeconds, StrictContract


class DialogueLine(StrictContract):
    character_id: UUID
    text: str = Field(min_length=1)
    delivery: str = ""


class ScriptSegment(StrictContract):
    segment_id: UUID
    sequence: int = Field(ge=0)
    plot_beat_ids: list[UUID] = Field(min_length=1)
    narration: str = Field(min_length=1)
    dialogue: list[DialogueLine] = Field(default_factory=list)
    visual_gags: list[str] = Field(default_factory=list)
    target_duration_seconds: PositiveSeconds
    measured_duration_seconds: PositiveSeconds | None = None


class RecapScript(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    project_id: UUID
    revision: int = Field(ge=1)
    title: str = Field(min_length=1)
    target_duration_seconds: PositiveSeconds
    humor_intensity: int = Field(ge=0, le=10)
    narrator_style: str
    segments: list[ScriptSegment] = Field(min_length=1)
    safety_notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def segment_order_is_unique(self) -> RecapScript:
        sequences = [segment.sequence for segment in self.segments]
        if len(sequences) != len(set(sequences)):
            raise ValueError("script segment sequences must be unique")
        return self
