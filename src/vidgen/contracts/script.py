"""Strict contracts for the T11 compression and comedy script pipeline.

T11 consumes the selected, validated T10 ``EpisodeAnalysis`` and produces a causally
complete ``CompressedPlotPlan``, an original comedy ``RecapScript``, deterministic
validation reports, and a structured editorial review. Every contract here forbids
extra fields and carries an explicit schema version.
"""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, model_validator

from vidgen.contracts.common import Score, StrictContract
from vidgen.contracts.episode_analysis import EpisodeAnalysis, SourceReference, StructuredNote

Version = Literal["1.0"]
RubricScore = Annotated[float, Field(ge=0, le=100)]
RecapMode = Literal["full_recap", "highlight_reel"]
StructuralRole = Literal[
    "setup", "inciting_incident", "escalation", "climax", "resolution", "supporting"
]
JokeType = Literal[
    "commentary",
    "analogy",
    "exaggeration",
    "contrast",
    "callback",
    "character_observation",
    "visual_gag",
    "wordplay",
]
SegmentType = Literal["NARRATION", "DIALOGUE", "PAUSE"]
SpeakerKind = Literal["narrator", "character", "anonymous"]
ApprovalRecommendation = Literal["approve", "revise", "reject"]


class ChannelVoiceConfig(StrictContract):
    schema_version: Version = "1.0"
    narrator_persona: str = Field(min_length=1)
    tone_keywords: list[str] = Field(default_factory=list)
    catchphrases: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Plot compression
# ---------------------------------------------------------------------------


class PlotCompressionRequest(StrictContract):
    schema_version: Version = "1.0"
    project_id: UUID
    episode_analysis_id: UUID
    episode_analysis: EpisodeAnalysis
    input_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    idempotency_key: str = Field(min_length=1)
    contract_version: str
    prompt_version: str
    provider_configuration_version: str
    target_duration_ms: int = Field(gt=0)
    target_words: int = Field(gt=0)
    target_words_per_minute: int = Field(gt=0)
    required_beat_ids: list[UUID] = Field(default_factory=list)
    excluded_topics: list[str] = Field(default_factory=list)
    recap_mode: RecapMode = "full_recap"
    provider_options: dict[str, str | int | float | bool] = Field(default_factory=dict)


class CompressedPlotBeat(StrictContract):
    schema_version: Version = "1.0"
    plot_beat_id: UUID
    sequence: int = Field(ge=1)
    summary: str = Field(min_length=1)
    structural_role: StructuralRole = "supporting"
    mandatory: bool
    payoff_score: Score
    character_ids: list[UUID] = Field(default_factory=list)
    scene_ids: list[UUID] = Field(default_factory=list)
    source_references: list[SourceReference] = Field(min_length=1)


class OmittedPlotBeat(StrictContract):
    schema_version: Version = "1.0"
    plot_beat_id: UUID
    reason: str = Field(min_length=1)
    may_cause_confusion: bool = False
    confusion_explanation: str | None = None

    @model_validator(mode="after")
    def confusion_requires_explanation(self) -> OmittedPlotBeat:
        if self.may_cause_confusion and not self.confusion_explanation:
            raise ValueError("an omission that risks confusion must explain why")
        return self


class ConnectiveExplanation(StrictContract):
    cause_beat_id: UUID
    effect_beat_id: UUID
    explanation: str = Field(min_length=1)


class BeatWordAllocation(StrictContract):
    plot_beat_id: UUID
    words: int = Field(gt=0)
    estimated_duration_ms: int = Field(gt=0)


class WordBudget(StrictContract):
    total_target_words: int = Field(gt=0)
    allocations: list[BeatWordAllocation] = Field(min_length=1)


class PacingAllocation(StrictContract):
    plot_beat_id: UUID
    estimated_duration_ms: int = Field(gt=0)


class CompressedPlotPlan(StrictContract):
    schema_version: Version = "1.0"
    plan_id: UUID
    project_id: UUID
    episode_analysis_id: UUID
    logline: str = Field(min_length=1)
    selected_beats: list[CompressedPlotBeat] = Field(min_length=1)
    omitted_beats: list[OmittedPlotBeat] = Field(default_factory=list)
    connective_explanations: list[ConnectiveExplanation] = Field(default_factory=list)
    pacing_plan: list[PacingAllocation] = Field(min_length=1)
    word_budget: WordBudget
    source_refs: list[SourceReference] = Field(default_factory=list)
    assumptions: list[StructuredNote] = Field(default_factory=list)
    warnings: list[StructuredNote] = Field(default_factory=list)

    @model_validator(mode="after")
    def beats_are_disjoint(self) -> CompressedPlotPlan:
        selected = [beat.plot_beat_id for beat in self.selected_beats]
        omitted = [beat.plot_beat_id for beat in self.omitted_beats]
        if len(selected) != len(set(selected)):
            raise ValueError("selected beats must be unique")
        if len(omitted) != len(set(omitted)):
            raise ValueError("omitted beats must be unique")
        if set(selected) & set(omitted):
            raise ValueError("a beat cannot be both selected and omitted")
        return self


class ScriptProviderMetadata(StrictContract):
    schema_version: Version = "1.0"
    provider: str
    model: str
    provider_request_id: str
    operation: Literal["compress_plot", "write_script", "edit_script"]
    attempt_number: int = Field(ge=1)
    input_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    prompt_version: str
    contract_version: str
    rubric_version: str | None = None
    redacted_response_metadata: dict[str, str | int | float | bool] = Field(default_factory=dict)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    warnings: list[StructuredNote] = Field(default_factory=list)
    validation_status: Literal["pending", "valid", "invalid"] = "pending"


class ProviderCompressedPlotResult(StrictContract):
    output: CompressedPlotPlan
    metadata: ScriptProviderMetadata


# ---------------------------------------------------------------------------
# Comedy writing
# ---------------------------------------------------------------------------


class TextSpan(StrictContract):
    start: int = Field(ge=0)
    end: int = Field(gt=0)

    @model_validator(mode="after")
    def end_after_start(self) -> TextSpan:
        if self.end <= self.start:
            raise ValueError("end must be greater than start")
        return self


class JokeAnnotation(StrictContract):
    schema_version: Version = "1.0"
    joke_id: UUID
    joke_type: JokeType
    setup_span: TextSpan | None = None
    punchline_span: TextSpan | None = None
    callback_id: UUID | None = None
    source_beat_ids: list[UUID] = Field(min_length=1)
    confidence: Score | None = None
    validation_status: Literal["pending", "valid", "invalid"] = "pending"

    @model_validator(mode="after")
    def callback_requires_type(self) -> JokeAnnotation:
        if self.callback_id is not None and self.joke_type != "callback":
            raise ValueError("only callback jokes may reference a callback_id")
        return self


class ScriptSegment(StrictContract):
    schema_version: Version = "1.0"
    segment_id: UUID
    sequence: int = Field(ge=0)
    type: SegmentType
    speaker_kind: SpeakerKind
    speaker_character_id: UUID | None = None
    anonymous_speaker_label: str | None = None
    text: str
    plot_beat_ids: list[UUID] = Field(min_length=1)
    source_scene_ids: list[UUID] = Field(default_factory=list)
    joke_annotations: list[JokeAnnotation] = Field(default_factory=list)
    visual_gag: str | None = None
    estimated_duration_ms: int = Field(gt=0)
    voice_direction: str = ""
    locked: bool = False
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def speaker_reference_matches_kind(self) -> ScriptSegment:
        if self.speaker_kind == "character" and self.speaker_character_id is None:
            raise ValueError("a character speaker requires speaker_character_id")
        if self.speaker_kind == "anonymous" and not self.anonymous_speaker_label:
            raise ValueError("an anonymous speaker requires anonymous_speaker_label")
        if self.speaker_kind == "narrator" and (
            self.speaker_character_id is not None or self.anonymous_speaker_label is not None
        ):
            raise ValueError("a narrator segment must not reference a character or label")
        if self.type in ("NARRATION", "DIALOGUE") and not self.text.strip():
            raise ValueError("narration and dialogue segments require nonempty text")
        return self


class Callback(StrictContract):
    schema_version: Version = "1.0"
    callback_id: UUID
    setup_segment_id: UUID
    payoff_segment_id: UUID
    description: str = Field(min_length=1)


CoverageClassification = Literal["covered", "partial", "missing"]


class BeatCoverage(StrictContract):
    schema_version: Version = "1.0"
    plot_beat_id: UUID
    segment_ids: list[UUID] = Field(default_factory=list)
    coverage: CoverageClassification
    mandatory: bool
    diagnostics: list[StructuredNote] = Field(default_factory=list)


class ComedyWritingRequest(StrictContract):
    schema_version: Version = "1.0"
    project_id: UUID
    episode_analysis_id: UUID
    compressed_plot_plan_id: UUID
    input_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    idempotency_key: str = Field(min_length=1)
    contract_version: str
    prompt_version: str
    provider_configuration_version: str
    compressed_plot: CompressedPlotPlan
    channel_voice: ChannelVoiceConfig
    humor_intensity: Score
    prohibited_patterns: list[str] = Field(default_factory=list)
    target_words: int = Field(gt=0)
    recap_mode: RecapMode = "full_recap"
    locked_segments: list[ScriptSegment] = Field(default_factory=list)
    revision_feedback: str | None = None
    provider_options: dict[str, str | int | float | bool] = Field(default_factory=dict)


class RecapScript(StrictContract):
    schema_version: Version = "1.0"
    script_id: UUID
    version: int = Field(ge=1)
    parent_script_id: UUID | None = None
    project_id: UUID
    episode_analysis_id: UUID
    compressed_plot_plan_id: UUID
    target_duration_ms: int = Field(gt=0)
    target_word_count: int = Field(gt=0)
    actual_word_count: int = Field(ge=0)
    voice_profile_ref: str = Field(min_length=1)
    humor_intensity: Score
    cold_open_text: str | None = None
    segments: list[ScriptSegment] = Field(min_length=1)
    callbacks: list[Callback] = Field(default_factory=list)
    beat_coverage: list[BeatCoverage] = Field(default_factory=list)
    source_refs: list[SourceReference] = Field(default_factory=list)
    assumptions: list[StructuredNote] = Field(default_factory=list)
    warnings: list[StructuredNote] = Field(default_factory=list)

    @model_validator(mode="after")
    def segments_are_well_ordered(self) -> RecapScript:
        sequences = [segment.sequence for segment in self.segments]
        if sequences != sorted(sequences) or len(sequences) != len(set(sequences)):
            raise ValueError("segment sequences must be unique and monotonic")
        ids = [segment.segment_id for segment in self.segments]
        if len(ids) != len(set(ids)):
            raise ValueError("segment IDs must be unique")
        return self


class ProviderRecapScriptResult(StrictContract):
    output: RecapScript
    metadata: ScriptProviderMetadata


# ---------------------------------------------------------------------------
# Deterministic validation
# ---------------------------------------------------------------------------


class ScriptValidationError(StrictContract):
    code: str
    entity_path: str
    invalid_value: str | int | float | bool | None = None
    explanation: str = Field(min_length=1)


class ScriptValidationReport(StrictContract):
    schema_version: Version = "1.0"
    valid: bool
    errors: list[ScriptValidationError] = Field(default_factory=list)
    warnings: list[StructuredNote] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Comedy editor
# ---------------------------------------------------------------------------


class ComedyRubric(StrictContract):
    schema_version: Version = "1.0"
    rubric_version: str
    dimensions: list[str] = Field(min_length=1)
    approval_overall_min: RubricScore = 85
    approval_plot_fidelity_min: RubricScore = 92


class ComedyRubricScores(StrictContract):
    schema_version: Version = "1.0"
    plot_fidelity: RubricScore
    clarity: RubricScore
    joke_density: RubricScore
    joke_variety: RubricScore
    punchline_placement: RubricScore
    spoken_rhythm: RubricScore
    pacing: RubricScore
    callback_quality: RubricScore
    repetition: RubricScore
    narratability: RubricScore
    overall: RubricScore


class ComedyIssue(StrictContract):
    segment_id: UUID | None = None
    description: str = Field(min_length=1)
    rubric_dimension: str
    severity: Literal["minor", "major", "blocking"] = "minor"


class ScriptEdit(StrictContract):
    segment_id: UUID
    old_text: str
    new_text: str
    reason: str = Field(min_length=1)
    rubric_dimensions: list[str] = Field(default_factory=list)
    plot_beat_ids: list[UUID] = Field(default_factory=list)
    changes_word_count: bool
    was_locked: bool


class ScriptDiff(StrictContract):
    schema_version: Version = "1.0"
    from_version: int | None = Field(default=None, ge=1)
    to_version: int = Field(ge=1)
    added_segment_ids: list[UUID] = Field(default_factory=list)
    removed_segment_ids: list[UUID] = Field(default_factory=list)
    changed_segments: list[ScriptEdit] = Field(default_factory=list)
    unchanged_segment_ids: list[UUID] = Field(default_factory=list)


class ComedyEditRequest(StrictContract):
    schema_version: Version = "1.0"
    project_id: UUID
    script_id: UUID
    script_version: int = Field(ge=1)
    recap_script: RecapScript
    compressed_plot: CompressedPlotPlan
    rubric: ComedyRubric
    prior_review_id: UUID | None = None
    attempt_number: int = Field(ge=1)
    input_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    idempotency_key: str = Field(min_length=1)
    contract_version: str
    prompt_version: str
    rubric_version: str
    provider_configuration_version: str


class ComedyEditResult(StrictContract):
    schema_version: Version = "1.0"
    scores: ComedyRubricScores
    issues: list[ComedyIssue] = Field(default_factory=list)
    edits: list[ScriptEdit] = Field(default_factory=list)
    revised_script: RecapScript
    approval_recommendation: ApprovalRecommendation


class ProviderComedyEditResult(StrictContract):
    output: ComedyEditResult
    metadata: ScriptProviderMetadata


class ScriptGenerationResult(StrictContract):
    schema_version: Version = "1.0"
    generation_run_id: UUID
    compressed_plot_plan_id: UUID | None = None
    script_id: UUID | None = None
    script_version: int | None = Field(default=None, ge=1)
    status: str
    validation_report: ScriptValidationReport | None = None
    review_scores: ComedyRubricScores | None = None
    revision_count: int = Field(default=0, ge=0)
