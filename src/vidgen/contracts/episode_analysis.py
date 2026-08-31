"""Strict contracts for the T10 scene-map and episode-reduce boundary."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from vidgen.contracts.common import StrictContract

Version = Literal["1.0"]
Confidence = float


class StructuredNote(StrictContract):
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)


class SourceReference(StrictContract):
    schema_version: Version = "1.0"
    reference_type: Literal[
        "transcript_segment", "speaker_turn", "source_scene", "frame", "contact_sheet", "project"
    ]
    reference_id: UUID
    scene_id: UUID | None = None
    start_ms: int | None = Field(default=None, ge=0)
    end_ms: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def interval(self) -> SourceReference:
        if (self.start_ms is None) != (self.end_ms is None):
            raise ValueError("source reference times must be supplied together")
        if self.start_ms is not None and self.end_ms is not None and self.end_ms <= self.start_ms:
            raise ValueError("end_ms must be greater than start_ms")
        return self


class AnalysisObservation(StrictContract):
    schema_version: Version = "1.0"
    claim: str = Field(min_length=1)
    source_references: list[SourceReference] = Field(min_length=1)


class AnalysisInference(AnalysisObservation):
    confidence: Confidence = Field(ge=0, le=1)


class AliasEvidence(StrictContract):
    alias: str = Field(min_length=1)
    source_references: list[SourceReference] = Field(min_length=1)


class CharacterCandidate(StrictContract):
    schema_version: Version = "1.0"
    character_id: UUID
    canonical_name: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    alias_evidence: list[AliasEvidence] = Field(default_factory=list)
    anonymous: bool = False
    confidence: Confidence = Field(ge=0, le=1)
    source_references: list[SourceReference] = Field(min_length=1)


class LocationCandidate(StrictContract):
    schema_version: Version = "1.0"
    location_id: UUID
    canonical_name: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    alias_evidence: list[AliasEvidence] = Field(default_factory=list)
    confidence: Confidence = Field(ge=0, le=1)
    source_references: list[SourceReference] = Field(min_length=1)


class StateEvent(StrictContract):
    schema_version: Version = "1.0"
    state_event_id: UUID
    entity_id: UUID
    scene_id: UUID
    sequence: int = Field(ge=1)
    description: str = Field(min_length=1)
    confidence: Confidence = Field(ge=0, le=1)
    source_references: list[SourceReference] = Field(min_length=1)


class Relationship(StrictContract):
    schema_version: Version = "1.0"
    relationship_id: UUID
    source_character_id: UUID
    target_character_id: UUID
    description: str = Field(min_length=1)
    confidence: Confidence = Field(ge=0, le=1)
    source_references: list[SourceReference] = Field(min_length=1)


class PlotBeat(StrictContract):
    schema_version: Version = "1.0"
    plot_beat_id: UUID
    sequence: int = Field(ge=1)
    scene_ids: list[UUID] = Field(min_length=1)
    character_ids: list[UUID] = Field(default_factory=list)
    summary: str = Field(min_length=1)
    importance: Confidence = Field(ge=0, le=1)
    payoff_score: Confidence = Field(ge=0, le=1)
    mandatory: bool
    source_references: list[SourceReference] = Field(default_factory=list)


class BeatDependency(StrictContract):
    cause_beat_id: UUID
    effect_beat_id: UUID
    source_references: list[SourceReference] = Field(min_length=1)


class UnresolvedAmbiguity(StrictContract):
    schema_version: Version = "1.0"
    ambiguity_id: UUID
    description: str = Field(min_length=1)
    candidate_ids: list[UUID] = Field(default_factory=list)
    source_references: list[SourceReference] = Field(min_length=1)


class CanonicalScene(StrictContract):
    scene_id: UUID
    sequence: int = Field(ge=1)
    source_start_ms: int = Field(ge=0)
    source_end_ms: int = Field(ge=1)
    summary: str = Field(min_length=1)
    dramatic_purpose: str = Field(min_length=1)
    character_ids: list[UUID] = Field(default_factory=list)
    location_id: UUID | None = None
    confidence: Confidence = Field(ge=0, le=1)
    source_references: list[SourceReference] = Field(min_length=1)

    @model_validator(mode="after")
    def interval(self) -> CanonicalScene:
        if self.source_end_ms <= self.source_start_ms:
            raise ValueError("source_end_ms must be greater than source_start_ms")
        return self


class CharacterAlias(StrictContract):
    """A character's known aliases, keyed by character identifier.

    OpenAI's structured-output strict mode does not support free-form dict
    schemas (``additionalProperties`` with a value schema), so the mapping that
    would otherwise be ``dict[str, list[str]]`` is expressed as a list of these
    pairs instead.
    """

    character_id: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)


class SceneAnalysisResult(StrictContract):
    schema_version: Version = "1.0"
    scene_id: UUID
    sequence: int = Field(ge=1)
    source_start_ms: int = Field(ge=0)
    source_end_ms: int = Field(ge=1)
    summary: str = Field(min_length=1)
    dramatic_purpose: str = Field(min_length=1)
    observed_characters: list[CharacterCandidate] = Field(default_factory=list)
    character_aliases: list[CharacterAlias] = Field(default_factory=list)
    anonymous_speaker_references: list[str] = Field(default_factory=list)
    location_candidates: list[LocationCandidate] = Field(default_factory=list)
    state_changes: list[StateEvent] = Field(default_factory=list)
    important_actions: list[AnalysisObservation] = Field(default_factory=list)
    candidate_plot_beats: list[PlotBeat] = Field(default_factory=list)
    candidate_causal_links: list[BeatDependency] = Field(default_factory=list)
    visual_motifs: list[str] = Field(default_factory=list)
    direct_observations: list[AnalysisObservation] = Field(default_factory=list)
    inferences: list[AnalysisInference] = Field(default_factory=list)
    confidence: Confidence = Field(ge=0, le=1)
    source_references: list[SourceReference] = Field(min_length=1)
    assumptions: list[StructuredNote] = Field(default_factory=list)
    warnings: list[StructuredNote] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)


class SceneAnalysisRequest(StrictContract):
    schema_version: Version = "1.0"
    project_id: UUID
    evidence_package_id: UUID
    scene_id: UUID
    sequence: int = Field(ge=1)
    source_start_ms: int = Field(ge=0)
    source_end_ms: int = Field(ge=1)
    input_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    idempotency_key: str = Field(min_length=1)
    contract_version: str
    prompt_version: str
    provider_configuration_version: str
    evidence_references: list[SourceReference] = Field(min_length=1, max_length=100)
    evidence_excerpts: list[SceneEvidenceExcerpt] = Field(default_factory=list, max_length=100)
    provider_options: dict[str, str | int | float | bool] = Field(default_factory=dict)


class SceneEvidenceExcerpt(StrictContract):
    text: str = Field(min_length=1, max_length=4000)
    speaker_label: str | None = None
    source_reference: SourceReference


class EpisodeSynthesisRequest(StrictContract):
    schema_version: Version = "1.0"
    project_id: UUID
    evidence_package_id: UUID
    source_video_id: UUID
    duration_ms: int = Field(ge=1)
    input_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    idempotency_key: str
    contract_version: str
    prompt_version: str
    provider_configuration_version: str
    scene_result_ids: list[UUID] = Field(min_length=1)
    scene_results: list[SceneAnalysisResult] = Field(min_length=1)
    validation_errors: list[AnalysisValidationError] = Field(default_factory=list)


class EpisodeAnalysis(StrictContract):
    schema_version: Version = "1.0"
    episode_id: UUID
    project_id: UUID
    source_video_id: UUID
    evidence_package_id: UUID
    title: str = ""
    duration_ms: int = Field(ge=1)
    logline: str = ""
    genre: list[str] = Field(default_factory=list)
    tone: list[str] = Field(default_factory=list)
    characters: list[CharacterCandidate] = Field(default_factory=list)
    locations: list[LocationCandidate] = Field(default_factory=list)
    scenes: list[CanonicalScene] = Field(min_length=1)
    state_events: list[StateEvent] = Field(default_factory=list)
    relationships: list[Relationship] = Field(default_factory=list)
    plot_beats: list[PlotBeat] = Field(default_factory=list)
    beat_dependencies: list[BeatDependency] = Field(default_factory=list)
    unresolved_ambiguities: list[UnresolvedAmbiguity] = Field(default_factory=list)
    source_references: list[SourceReference] = Field(min_length=1)
    assumptions: list[StructuredNote] = Field(default_factory=list)
    warnings: list[StructuredNote] = Field(default_factory=list)


class AnalysisValidationError(StrictContract):
    code: str
    entity_path: str
    invalid_value: str | int | float | bool | None = None
    source_reference: SourceReference | None = None
    explanation: str


class AnalysisValidationReport(StrictContract):
    schema_version: Version = "1.0"
    valid: bool
    errors: list[AnalysisValidationError] = Field(default_factory=list)
    warnings: list[StructuredNote] = Field(default_factory=list)


class EpisodeAnalysisResult(StrictContract):
    schema_version: Version = "1.0"
    analysis_run_id: UUID
    episode_analysis_id: UUID | None = None
    analysis_asset_id: UUID | None = None
    version: int | None = Field(default=None, ge=1)
    validation_report: AnalysisValidationReport


class ProviderMetadata(StrictContract):
    provider: str
    model: str
    provider_request_id: str
    attempt_number: int = Field(ge=1)
    prompt_version: str
    contract_version: str
    input_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    redacted_response_metadata: dict[str, str | int | float | bool] = Field(default_factory=dict)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    warnings: list[StructuredNote] = Field(default_factory=list)
    validation_status: Literal["pending", "valid", "invalid"] = "pending"


class ProviderSceneAnalysisResult(StrictContract):
    output: SceneAnalysisResult
    metadata: ProviderMetadata


class ProviderEpisodeAnalysisResult(StrictContract):
    output: EpisodeAnalysis
    metadata: ProviderMetadata
