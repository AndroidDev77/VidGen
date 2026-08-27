"""Versioned, provider-neutral contracts for T13 storyboard generation and timing.

T13 owns two clearly separated concerns. The Storyboard Director is a creative
provider that proposes semantic shots; the deterministic retimer owns final
timing. Every canonical timing value in this module is an exact integer count of
microseconds so that identical inputs hash identically and no binary
floating-point drift can accumulate across a run.

``ShotDefinition``/``Storyboard`` were reserved as placeholders by the T01
scaffolding. T13 is the first task to populate them, so this module replaces the
placeholder shapes with the canonical contracts rather than adding a competing
representation.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import Field, model_validator

from vidgen.contracts.common import StrictContract
from vidgen.contracts.episode_analysis import StructuredNote

CONTRACT_VERSION = "storyboard/1.0"
MICROSECONDS_PER_SECOND = 1_000_000

Microseconds = Annotated[int, Field(ge=0)]
PositiveMicroseconds = Annotated[int, Field(gt=0)]
Sha256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Sequence = Annotated[int, Field(ge=0)]

CameraFraming = Literal[
    "extreme_wide",
    "wide",
    "medium_wide",
    "medium",
    "medium_close",
    "close_up",
    "extreme_close_up",
    "insert",
]
CameraAngle = Literal[
    "eye_level",
    "low_angle",
    "high_angle",
    "overhead",
    "dutch",
    "over_the_shoulder",
    "point_of_view",
]
CameraMovement = Literal[
    "static",
    "pan_left",
    "pan_right",
    "tilt_up",
    "tilt_down",
    "dolly_in",
    "dolly_out",
    "tracking",
    "crane",
    "handheld",
    "zoom_in",
    "zoom_out",
]
MovementIntensity = Literal["none", "subtle", "moderate", "strong"]
BeatIntent = Literal["establish", "react", "reveal", "punchline", "continue", "insert"]
TransitionKind = Literal["cut", "dissolve", "fade_in", "fade_out", "wipe", "match_cut", "whip_pan"]
TimeOfDay = Literal["dawn", "morning", "midday", "afternoon", "dusk", "night", "unspecified"]
ScreenPosition = Literal["left", "center_left", "center", "center_right", "right", "offscreen"]
ScreenDirection = Literal["neutral", "left_to_right", "right_to_left"]
TrimmingPolicy = Literal["trim_end", "trim_start", "trim_center", "none"]
BoundaryKind = Literal["sentence", "clause", "beat", "word"]


class VisualProviderCapability(StrictContract):
    """What a downstream T14/T15 visual provider can actually generate.

    T13 plans against this profile without ever calling the visual provider. The
    timing solver reads only this contract, so no single provider's limitations
    are hardcoded into the solver.
    """

    schema_version: Literal["1.0"] = "1.0"
    capability_profile_id: str = Field(min_length=1, max_length=128)
    profile_version: int = Field(gt=0)
    provider: str = Field(min_length=1, max_length=64)
    model_family: str = Field(min_length=1, max_length=128)
    # Empty means the provider generates continuous durations on
    # ``duration_increment_us`` steps between the min and max.
    supported_generation_durations_us: list[PositiveMicroseconds] = Field(default_factory=list)
    min_generation_duration_us: PositiveMicroseconds
    max_generation_duration_us: PositiveMicroseconds
    duration_increment_us: PositiveMicroseconds
    supported_aspect_ratios: list[str] = Field(min_length=1)
    supported_resolutions: list[str] = Field(min_length=1)
    max_characters_per_shot: int = Field(ge=0)
    max_reference_images: int = Field(ge=0)
    supports_camera_motion: bool
    supported_camera_movements: list[CameraMovement] = Field(default_factory=list)
    supported_transitions: list[TransitionKind] = Field(min_length=1)
    supports_image_to_video: bool
    supports_text_to_video: bool
    supports_continuity_seed: bool
    trimming_policy: TrimmingPolicy
    capability_hash: Sha256

    @model_validator(mode="after")
    def bounds_are_consistent(self) -> VisualProviderCapability:
        if self.max_generation_duration_us < self.min_generation_duration_us:
            raise ValueError("max_generation_duration_us must not be below the minimum")
        durations = self.supported_generation_durations_us
        if durations:
            if sorted(durations) != durations or len(set(durations)) != len(durations):
                raise ValueError("supported_generation_durations_us must be sorted and unique")
            if durations[0] != self.min_generation_duration_us:
                raise ValueError("the smallest supported duration must equal the minimum")
            if durations[-1] != self.max_generation_duration_us:
                raise ValueError("the largest supported duration must equal the maximum")
        if self.supports_camera_motion and not self.supported_camera_movements:
            raise ValueError("camera-motion providers must enumerate supported movements")
        if not self.supports_image_to_video and not self.supports_text_to_video:
            raise ValueError("a visual provider must support image-to-video or text-to-video")
        return self

    def is_supported_duration(self, duration_us: int) -> bool:
        if duration_us < self.min_generation_duration_us:
            return False
        if duration_us > self.max_generation_duration_us:
            return False
        if self.supported_generation_durations_us:
            return duration_us in self.supported_generation_durations_us
        offset = duration_us - self.min_generation_duration_us
        return offset % self.duration_increment_us == 0


class CameraPlan(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    framing: CameraFraming
    angle: CameraAngle
    movement: CameraMovement
    movement_intensity: MovementIntensity = "subtle"
    lens_note: str = Field(default="", max_length=512)

    @model_validator(mode="after")
    def static_shots_have_no_intensity(self) -> CameraPlan:
        if self.movement == "static" and self.movement_intensity != "none":
            raise ValueError("a static camera must declare movement_intensity 'none'")
        if self.movement != "static" and self.movement_intensity == "none":
            raise ValueError("a moving camera must declare a non-none movement_intensity")
        return self


class ActionPlan(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    subject_action: str = Field(min_length=1, max_length=1024)
    secondary_action: str = Field(default="", max_length=1024)
    beat_intent: BeatIntent
    staging_note: str = Field(default="", max_length=1024)
    prop_references: list[str] = Field(default_factory=list, max_length=16)


class TransitionPlan(StrictContract):
    """A transition between two canonical shots.

    ``handle_us`` is extra *generated* material requested from the visual
    provider so the transition has something to work with. It is never narration
    coverage: usable shot intervals stay contiguous and non-overlapping, and the
    handle is accounted for separately in the timing manifest.
    """

    schema_version: Literal["1.0"] = "1.0"
    kind: TransitionKind
    duration_us: Microseconds = 0
    handle_us: Microseconds = 0
    note: str = Field(default="", max_length=512)

    @model_validator(mode="after")
    def cuts_are_instantaneous(self) -> TransitionPlan:
        if self.kind == "cut" and (self.duration_us or self.handle_us):
            raise ValueError("a cut has no transition duration or handle")
        if self.kind != "cut" and self.duration_us <= 0:
            raise ValueError("a non-cut transition must declare a positive duration")
        if self.handle_us < self.duration_us and self.kind != "cut":
            raise ValueError("a non-cut transition handle must cover its duration")
        return self


class CharacterAppearanceState(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    character_id: UUID
    appearance_state_id: str = Field(min_length=1, max_length=128)
    wardrobe_state: str = Field(default="default", max_length=255)
    injury_state: str = Field(default="none", max_length=255)
    emotional_state: str = Field(default="neutral", max_length=255)


class PropState(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    prop_id: str = Field(min_length=1, max_length=128)
    owner_character_id: UUID | None = None
    note: str = Field(default="", max_length=512)


class SubjectPosition(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    character_id: UUID
    screen_position: ScreenPosition
    facing: ScreenDirection = "neutral"


class ContinuityState(StrictContract):
    """Structured continuity, never prose alone."""

    schema_version: Literal["1.0"] = "1.0"
    present_character_ids: list[UUID] = Field(default_factory=list)
    character_appearance_states: list[CharacterAppearanceState] = Field(default_factory=list)
    location_id: UUID | None = None
    sub_location: str = Field(default="", max_length=255)
    time_of_day: TimeOfDay = "unspecified"
    props: list[PropState] = Field(default_factory=list)
    subject_positions: list[SubjectPosition] = Field(default_factory=list)
    screen_direction: ScreenDirection = "neutral"
    emotional_state: str = Field(default="neutral", max_length=255)
    environment_conditions: list[str] = Field(default_factory=list, max_length=16)
    previous_shot_id: UUID | None = None
    unresolved_warnings: list[StructuredNote] = Field(default_factory=list)

    @model_validator(mode="after")
    def references_are_consistent(self) -> ContinuityState:
        present = set(self.present_character_ids)
        if len(present) != len(self.present_character_ids):
            raise ValueError("present_character_ids must be unique")
        for state in self.character_appearance_states:
            if state.character_id not in present:
                raise ValueError("appearance state references an absent character")
        for position in self.subject_positions:
            if position.character_id not in present:
                raise ValueError("subject position references an absent character")
        return self


class StoryboardSourceReference(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    reference_type: Literal[
        "evidence_package",
        "scene_evidence",
        "script_segment",
        "narration_segment",
        "narration_asset",
        "plot_beat",
        "character",
        "location",
        "episode_model",
    ]
    reference_id: UUID
    start_us: Microseconds | None = None
    end_us: Microseconds | None = None
    note: str = Field(default="", max_length=512)

    @model_validator(mode="after")
    def end_after_start(self) -> StoryboardSourceReference:
        if self.start_us is not None and self.end_us is not None and self.end_us <= self.start_us:
            raise ValueError("end_us must be greater than start_us")
        return self


class NarrationBoundary(StrictContract):
    """An approved split point inside one narration segment."""

    schema_version: Literal["1.0"] = "1.0"
    word_index: Sequence
    offset_us: Microseconds
    kind: BoundaryKind
    label: str = Field(default="", max_length=255)


class StoryboardShotProposal(StrictContract):
    """A creative proposal. It carries a desired duration, never authority."""

    schema_version: Literal["1.0"] = "1.0"
    proposal_sequence: Sequence
    visual_objective: str = Field(min_length=1, max_length=2048)
    desired_duration_us: PositiveMicroseconds
    word_start_index: Sequence
    word_end_index: int = Field(gt=0)
    clause_label: str = Field(default="", max_length=255)
    importance: float = Field(default=0.5, ge=0, le=1)
    camera: CameraPlan
    action: ActionPlan
    transition_in: TransitionPlan
    transition_out: TransitionPlan
    character_reference_ids: list[UUID] = Field(default_factory=list)
    location_reference_id: UUID | None = None
    evidence_references: list[StoryboardSourceReference] = Field(default_factory=list)
    incoming_continuity: ContinuityState
    expected_outgoing_continuity: ContinuityState
    warnings: list[StructuredNote] = Field(default_factory=list)

    @model_validator(mode="after")
    def word_range_is_ordered(self) -> StoryboardShotProposal:
        if self.word_end_index <= self.word_start_index:
            raise ValueError("word_end_index must be greater than word_start_index")
        return self


class StoryboardShot(StrictContract):
    """One canonical, fully timed shot."""

    schema_version: Literal["1.0"] = "1.0"
    shot_id: UUID
    storyboard_run_id: UUID
    segment_id: UUID
    global_sequence: Sequence
    segment_sequence: Sequence
    script_segment_id: UUID
    narration_segment_id: UUID
    # Segment-relative timing, then the same interval on the project timeline.
    start_us: Microseconds
    end_us: PositiveMicroseconds
    global_start_us: Microseconds
    global_end_us: PositiveMicroseconds
    usable_duration_us: PositiveMicroseconds
    requested_generation_duration_us: PositiveMicroseconds
    trim_start_us: Microseconds = 0
    trim_end_us: Microseconds = 0
    transition_handle_us: Microseconds = 0
    word_start_index: Sequence
    word_end_index: int = Field(gt=0)
    clause_label: str = Field(default="", max_length=255)
    visual_objective: str = Field(min_length=1, max_length=2048)
    camera: CameraPlan
    action: ActionPlan
    character_reference_ids: list[UUID] = Field(default_factory=list)
    location_reference_id: UUID | None = None
    prop_references: list[str] = Field(default_factory=list, max_length=16)
    evidence_references: list[StoryboardSourceReference] = Field(default_factory=list)
    transition_in: TransitionPlan
    transition_out: TransitionPlan
    incoming_continuity: ContinuityState
    expected_outgoing_continuity: ContinuityState
    capability_profile_id: str = Field(min_length=1, max_length=128)
    capability_hash: Sha256
    warnings: list[StructuredNote] = Field(default_factory=list)
    provenance: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def timing_is_exact(self) -> StoryboardShot:
        if self.end_us <= self.start_us:
            raise ValueError("end_us must be greater than start_us")
        if self.global_end_us <= self.global_start_us:
            raise ValueError("global_end_us must be greater than global_start_us")
        if self.end_us - self.start_us != self.usable_duration_us:
            raise ValueError("usable_duration_us must equal end_us minus start_us")
        if self.global_end_us - self.global_start_us != self.usable_duration_us:
            raise ValueError("global interval must equal the segment-relative interval")
        if self.word_end_index <= self.word_start_index:
            raise ValueError("word_end_index must be greater than word_start_index")
        expected_trim = self.requested_generation_duration_us - self.usable_duration_us
        if expected_trim < 0:
            raise ValueError("requested generation duration must cover the usable duration")
        if self.trim_start_us + self.trim_end_us != expected_trim:
            raise ValueError("trim values must account for the entire unused generated duration")
        return self


class StoryboardSegment(StrictContract):
    """One narration segment's canonical storyboard projection."""

    schema_version: Literal["1.0"] = "1.0"
    segment_id: UUID
    storyboard_run_id: UUID
    script_segment_id: UUID
    narration_segment_id: UUID
    sequence: Sequence
    narration_duration_us: PositiveMicroseconds
    global_start_us: Microseconds
    input_hash: Sha256
    shot_count: int = Field(gt=0)
    attempt_count: int = Field(ge=1)
    repair_attempt_count: int = Field(ge=0)
    warnings: list[StructuredNote] = Field(default_factory=list)


class TimingAdjustment(StrictContract):
    """Every deviation from the Director's proposal to canonical timing."""

    schema_version: Literal["1.0"] = "1.0"
    segment_sequence: Sequence
    proposal_sequence: int = Field(ge=-1)
    shot_sequence: int = Field(ge=-1)
    kind: Literal[
        "boundary_snap",
        "split",
        "merge",
        "clamp_min",
        "clamp_max",
        "residual_allocation",
        "final_end_snap",
        "generation_round_up",
        "trim",
    ]
    proposed_duration_us: int
    canonical_duration_us: int
    delta_us: int
    reason: str = Field(min_length=1, max_length=512)

    @model_validator(mode="after")
    def delta_matches(self) -> TimingAdjustment:
        if self.delta_us != self.canonical_duration_us - self.proposed_duration_us:
            raise ValueError("delta_us must equal canonical minus proposed duration")
        return self


class TimingManifestEntry(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    shot_id: UUID
    global_sequence: Sequence
    segment_sequence: Sequence
    script_segment_id: UUID
    narration_segment_id: UUID
    global_start_us: Microseconds
    global_end_us: PositiveMicroseconds
    usable_duration_us: PositiveMicroseconds
    requested_generation_duration_us: PositiveMicroseconds
    trim_start_us: Microseconds
    trim_end_us: Microseconds
    transition_handle_us: Microseconds


class TimingManifest(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    storyboard_run_id: UUID
    project_id: UUID
    script_id: UUID
    script_version: int = Field(gt=0)
    narration_run_id: UUID
    capability_profile_id: str = Field(min_length=1, max_length=128)
    capability_hash: Sha256
    retimer_version: str = Field(min_length=1, max_length=32)
    contract_version: str = Field(min_length=1, max_length=32)
    # Cumulative narration offsets; length is the segment count plus one.
    segment_boundaries_us: list[Microseconds] = Field(min_length=2)
    total_narration_duration_us: PositiveMicroseconds
    total_usable_duration_us: PositiveMicroseconds
    total_requested_generation_duration_us: PositiveMicroseconds
    total_transition_handle_us: Microseconds = 0
    residual_allocation_us: int = 0
    entries: list[TimingManifestEntry] = Field(min_length=1)
    adjustments: list[TimingAdjustment] = Field(default_factory=list)
    warnings: list[StructuredNote] = Field(default_factory=list)

    @model_validator(mode="after")
    def coverage_is_complete(self) -> TimingManifest:
        if self.segment_boundaries_us[0] != 0:
            raise ValueError("segment boundaries must start at zero")
        if self.segment_boundaries_us != sorted(self.segment_boundaries_us):
            raise ValueError("segment boundaries must be non-decreasing")
        if self.segment_boundaries_us[-1] != self.total_narration_duration_us:
            raise ValueError("the last segment boundary must equal the narration duration")
        if self.total_usable_duration_us != self.total_narration_duration_us:
            raise ValueError("usable shot duration must exactly cover measured narration")
        cursor = 0
        for entry in self.entries:
            if entry.global_start_us != cursor:
                raise ValueError("timing manifest entries must be gapless and non-overlapping")
            cursor = entry.global_end_us
        if cursor != self.total_narration_duration_us:
            raise ValueError("the final shot must end at the measured narration duration")
        return self


class StoryboardValidationDiagnostic(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    code: Literal[
        "narration_coverage_gap",
        "invalid_overlap",
        "impossible_duration_allocation",
        "unsupported_provider_duration",
        "excessive_character_count",
        "too_many_references",
        "missing_continuity_state",
        "invalid_character_reference",
        "invalid_location_reference",
        "missing_evidence_reference",
        "provider_schema_failure",
        "continuity_contradiction",
        "unsupported_camera_movement",
        "unsupported_transition",
        "nonpositive_duration",
        "word_range_gap",
    ]
    severity: Literal["error", "warning"]
    repairable: bool
    message: str = Field(min_length=1, max_length=1024)
    entity_path: str = Field(min_length=1, max_length=255)
    segment_sequence: int = Field(ge=-1, default=-1)
    shot_sequence: int = Field(ge=-1, default=-1)
    measured_us: int | None = None
    expected_us: int | None = None


class StoryboardValidationReport(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    valid: bool
    diagnostics: list[StoryboardValidationDiagnostic] = Field(default_factory=list)
    checked_segment_sequences: list[Sequence] = Field(default_factory=list)
    covered_duration_us: Microseconds = 0
    expected_duration_us: Microseconds = 0

    @model_validator(mode="after")
    def validity_matches_diagnostics(self) -> StoryboardValidationReport:
        errors = any(item.severity == "error" for item in self.diagnostics)
        if self.valid and errors:
            raise ValueError("a report with error diagnostics cannot be valid")
        return self


class StoryboardProviderRequest(StrictContract):
    """The immutable envelope handed to a Storyboard Director.

    It carries IDs, measured timing, and structured references. It never carries
    credentials and never carries raw provider payloads.
    """

    schema_version: Literal["1.0"] = "1.0"
    idempotency_key: str = Field(min_length=1, max_length=255)
    project_id: UUID
    episode_model_id: UUID
    episode_model_hash: Sha256
    script_id: UUID
    script_version: int = Field(gt=0)
    script_segment_id: UUID
    segment_sequence: Sequence
    narration_run_id: UUID
    narration_segment_id: UUID
    narration_asset_id: UUID
    measured_duration_us: PositiveMicroseconds
    narration_text: str = Field(min_length=1, max_length=8000)
    word_timings: list[NarrationBoundary] = Field(min_length=1)
    approved_boundaries: list[NarrationBoundary] = Field(default_factory=list)
    evidence_references: list[StoryboardSourceReference] = Field(
        default_factory=list, max_length=64
    )
    available_character_ids: list[UUID] = Field(default_factory=list)
    available_location_ids: list[UUID] = Field(default_factory=list)
    anonymous_speaker_label: str | None = None
    incoming_continuity: ContinuityState
    capability: VisualProviderCapability
    contract_version: str = Field(min_length=1, max_length=32)
    prompt_version: str = Field(min_length=1, max_length=32)
    provider_options: dict[str, str | int | float | bool] = Field(default_factory=dict)
    validation_diagnostics: list[StoryboardValidationDiagnostic] = Field(default_factory=list)
    trace_context: dict[str, str] = Field(default_factory=dict)
    attempt_number: int = Field(ge=1, le=8)


class StoryboardProviderResult(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    proposals: list[StoryboardShotProposal] = Field(min_length=1)
    expected_incoming_continuity: ContinuityState
    expected_outgoing_continuity: ContinuityState
    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=128)
    provider_request_id: str = Field(min_length=1, max_length=255)
    idempotency_key: str = Field(min_length=1, max_length=255)
    attempt_number: int = Field(ge=1, le=8)
    usage: dict[str, int | float] = Field(default_factory=dict)
    redacted_response_metadata: dict[str, str | int | float | bool] = Field(default_factory=dict)
    warnings: list[StructuredNote] = Field(default_factory=list)

    @model_validator(mode="after")
    def proposals_are_ordered(self) -> StoryboardProviderResult:
        sequences = [proposal.proposal_sequence for proposal in self.proposals]
        if sequences != list(range(len(sequences))):
            raise ValueError("proposal_sequence must be dense and start at zero")
        return self


class Storyboard(StrictContract):
    """The canonical, versioned T13 output stored through AssetService."""

    schema_version: Literal["1.0"] = "1.0"
    storyboard_id: UUID
    storyboard_run_id: UUID
    project_id: UUID
    version: int = Field(gt=0)
    episode_model_id: UUID
    episode_model_hash: Sha256
    script_id: UUID
    script_version: int = Field(gt=0)
    script_hash: Sha256
    narration_run_id: UUID
    capability_profile_id: str = Field(min_length=1, max_length=128)
    capability_hash: Sha256
    contract_version: str = Field(min_length=1, max_length=32)
    director_version: str = Field(min_length=1, max_length=32)
    prompt_version: str = Field(min_length=1, max_length=32)
    retimer_version: str = Field(min_length=1, max_length=32)
    input_hash: Sha256
    total_duration_us: PositiveMicroseconds
    segments: list[StoryboardSegment] = Field(min_length=1)
    shots: list[StoryboardShot] = Field(min_length=1)
    warnings: list[StructuredNote] = Field(default_factory=list)
    provenance: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def shots_are_ordered_and_unique(self) -> Storyboard:
        sequences = [shot.global_sequence for shot in self.shots]
        if sequences != list(range(len(sequences))):
            raise ValueError("global_sequence must be dense and start at zero")
        if len({shot.shot_id for shot in self.shots}) != len(self.shots):
            raise ValueError("shot IDs must be unique")
        cursor = 0
        for shot in self.shots:
            if shot.global_start_us != cursor:
                raise ValueError("canonical shots must be gapless and non-overlapping")
            cursor = shot.global_end_us
        if cursor != self.total_duration_us:
            raise ValueError("the final shot must end at the total measured duration")
        segment_sequences = [segment.sequence for segment in self.segments]
        if segment_sequences != sorted(set(segment_sequences)):
            raise ValueError("segment sequences must be unique and ordered")
        return self


class StoryboardResult(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    storyboard_run_id: UUID
    project_id: UUID
    status: Literal["storyboard_complete", "storyboard_failed"]
    selected: bool = False
    storyboard_id: UUID | None = None
    storyboard_asset_id: UUID | None = None
    timing_manifest_asset_id: UUID | None = None
    validation_report_asset_id: UUID | None = None
    segment_count: int = Field(ge=0)
    shot_count: int = Field(ge=0)
    total_duration_us: Microseconds = 0
    repair_attempt_count: int = Field(ge=0, default=0)
    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=128)
    estimated_cost: str = "0"
    actual_cost: str = "0"
    currency: str = Field(default="USD", min_length=3, max_length=3)
    error_code: str | None = Field(default=None, max_length=128)
    warnings: list[StructuredNote] = Field(default_factory=list)
