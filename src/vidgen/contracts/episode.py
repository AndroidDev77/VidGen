"""Canonical episode analysis contracts."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from vidgen.contracts.common import NonNegativeSeconds, Score, StrictContract


class WardrobeState(StrictContract):
    state_id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    colors: list[str] = Field(default_factory=list)
    accessories: list[str] = Field(default_factory=list)


class CharacterDefinition(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    character_id: UUID
    canonical_name: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    role: Literal["protagonist", "antagonist", "supporting", "minor"]
    appearance: str = Field(min_length=1)
    face: str
    hair: str
    skin_tone: str
    body_proportions: str
    default_wardrobe_state: str
    wardrobe_states: list[WardrobeState] = Field(min_length=1)
    signature_accessories: list[str] = Field(default_factory=list)
    personality: list[str] = Field(default_factory=list)
    reference_asset_ids: list[UUID] = Field(default_factory=list)

    @model_validator(mode="after")
    def default_wardrobe_exists(self) -> CharacterDefinition:
        states = {state.state_id for state in self.wardrobe_states}
        if self.default_wardrobe_state not in states:
            raise ValueError("default_wardrobe_state must reference a wardrobe state")
        return self


class CharacterState(StrictContract):
    character_id: UUID
    wardrobe_state: str
    injury_state: str = "none"
    emotional_state: str = "neutral"
    location_id: UUID
    scene_id: UUID
    props: list[str] = Field(default_factory=list)


class LocationDefinition(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    location_id: UUID
    canonical_name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    layout: str
    palette: list[str] = Field(default_factory=list)
    fixed_features: list[str] = Field(default_factory=list)
    reference_asset_ids: list[UUID] = Field(default_factory=list)


class SceneDefinition(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    scene_id: UUID
    sequence: int = Field(ge=0)
    source_start_seconds: NonNegativeSeconds
    source_end_seconds: NonNegativeSeconds
    location_id: UUID | None = None
    character_ids: list[UUID] = Field(default_factory=list)
    summary: str = Field(min_length=1)
    actions: list[str] = Field(default_factory=list)
    dialogue_summary: str = ""
    representative_frame_asset_ids: list[UUID] = Field(default_factory=list)
    confidence: Score

    @model_validator(mode="after")
    def end_after_start(self) -> SceneDefinition:
        if self.source_end_seconds <= self.source_start_seconds:
            raise ValueError("source_end_seconds must be greater than source_start_seconds")
        return self


class PlotBeat(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    plot_beat_id: UUID
    sequence: int = Field(ge=0)
    scene_ids: list[UUID] = Field(min_length=1)
    summary: str = Field(min_length=1)
    importance: Score
    required_for_coherence: bool
    setup_ids: list[UUID] = Field(default_factory=list)
    payoff_ids: list[UUID] = Field(default_factory=list)


class EpisodeAnalysis(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    project_id: UUID
    title: str = Field(min_length=1)
    source_duration_seconds: NonNegativeSeconds
    logline: str = Field(min_length=1)
    genre: list[str] = Field(default_factory=list)
    themes: list[str] = Field(default_factory=list)
    characters: list[CharacterDefinition]
    locations: list[LocationDefinition]
    scenes: list[SceneDefinition]
    plot_beats: list[PlotBeat]
    unresolved_ambiguities: list[str] = Field(default_factory=list)
