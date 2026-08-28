"""Strict, versioned T20 semantic visual-QA contracts.

These contracts cross the boundaries between authoritative input selection,
deterministic sampling, deterministic media checks, the provider-neutral visual
agent, score recomputation, adjudication, persistence and the API projection.

Three rules shape every model here:

* A provider never supplies the canonical score. :class:`VisualQAProviderResult`
  carries dimension scores and proposals; :class:`VisualQAScore` is recomputed by
  application code from validated dimension results.
* A hard failure is structurally separate from a warning, so no averaging,
  provider confidence, or high score in another dimension can hide one.
* Evidence is required for every actionable finding, so a repair consumer (T21)
  always receives an exact frame reference and timestamp.

Nothing here may carry credentials, signed URLs, image or video bytes, or
unrestricted model reasoning. Provider metadata stays in the provider models and
is never merged into the canonical result.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from vidgen.contracts.common import StrictContract

CONTRACT_VERSION = "visual-qa/1.0"

Sha256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Confidence = Annotated[float, Field(ge=0, le=1)]
RawScore = Annotated[float, Field(ge=0, le=100)]
Weight = Annotated[float, Field(ge=0, le=100)]
Microseconds = Annotated[int, Field(ge=0)]


class VisualQATargetType(StrEnum):
    """Keyframe and video QA are independent identities, never one merged run."""

    KEYFRAME = "keyframe"
    VIDEO = "video"


class VisualQAOutcome(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    REVIEW = "REVIEW"


class VisualQARoutingRecommendation(StrEnum):
    """A recommendation only. T20 never executes a repair or a reroute."""

    NONE = "NONE"
    TARGETED_REPAIR = "TARGETED_REPAIR"
    PROMPT_SIMPLIFICATION = "PROMPT_SIMPLIFICATION"
    NEW_SEED = "NEW_SEED"
    COMPOSITION_SPLIT = "COMPOSITION_SPLIT"
    HUMAN_REVIEW = "HUMAN_REVIEW"


class VisualQAShotImportance(StrEnum):
    UTILITY = "utility"
    NORMAL = "normal"
    HERO = "hero"


class VisualQADimension(StrEnum):
    CHARACTER_IDENTITY = "character_identity"
    CHARACTER_COUNT = "character_count"
    LOCATION = "location"
    WARDROBE_AND_STATE = "wardrobe_and_state"
    ACTION_AND_MOTION = "action_and_motion"
    COMPOSITION = "composition"
    ANATOMY_AND_ARTIFACTS = "anatomy_and_artifacts"
    CONTINUITY_AND_STYLE = "continuity_and_style"


class VisualQARepairCode(StrEnum):
    """The bounded taxonomy T21 consumes. Adding a member is a version change."""

    WRONG_CHARACTER_IDENTITY = "WRONG_CHARACTER_IDENTITY"
    MISSING_PRIMARY_CHARACTER = "MISSING_PRIMARY_CHARACTER"
    EXTRA_CHARACTER = "EXTRA_CHARACTER"
    WRONG_CHARACTER_COUNT = "WRONG_CHARACTER_COUNT"
    WRONG_WARDROBE = "WRONG_WARDROBE"
    WRONG_CHARACTER_STATE = "WRONG_CHARACTER_STATE"
    WRONG_LOCATION = "WRONG_LOCATION"
    WRONG_LOCATION_STATE = "WRONG_LOCATION_STATE"
    MISSING_REQUIRED_PROP = "MISSING_REQUIRED_PROP"
    WRONG_PROP_OWNERSHIP = "WRONG_PROP_OWNERSHIP"
    MISSING_MANDATORY_ACTION = "MISSING_MANDATORY_ACTION"
    WRONG_ACTION = "WRONG_ACTION"
    INSUFFICIENT_MOTION = "INSUFFICIENT_MOTION"
    EXCESSIVE_MOTION = "EXCESSIVE_MOTION"
    CAMERA_PLAN_MISMATCH = "CAMERA_PLAN_MISMATCH"
    COMPOSITION_MISMATCH = "COMPOSITION_MISMATCH"
    SCREEN_DIRECTION_CONTRADICTION = "SCREEN_DIRECTION_CONTRADICTION"
    FACE_BREAKAGE = "FACE_BREAKAGE"
    ANATOMY_BREAKAGE = "ANATOMY_BREAKAGE"
    UNINTENDED_TEXT = "UNINTENDED_TEXT"
    STYLE_DRIFT = "STYLE_DRIFT"
    CONTINUITY_BREAK = "CONTINUITY_BREAK"
    BLACK_VIDEO = "BLACK_VIDEO"
    EXCESSIVE_FREEZE = "EXCESSIVE_FREEZE"
    EXCESSIVE_FLICKER = "EXCESSIVE_FLICKER"
    DURATION_MISMATCH = "DURATION_MISMATCH"
    DECODE_FAILURE = "DECODE_FAILURE"
    PROMPT_TOO_COMPLEX = "PROMPT_TOO_COMPLEX"
    TOO_MANY_CHARACTERS = "TOO_MANY_CHARACTERS"
    TOO_MANY_REFERENCES = "TOO_MANY_REFERENCES"
    AMBIGUOUS_VISUAL_EVIDENCE = "AMBIGUOUS_VISUAL_EVIDENCE"
    HUMAN_REVIEW_REQUIRED = "HUMAN_REVIEW_REQUIRED"


class VisualQASampleType(StrEnum):
    """Why the deterministic sampler selected a timestamp."""

    KEYFRAME_IMAGE = "keyframe_image"
    FIRST_FRAME = "first_frame"
    LAST_FRAME = "last_frame"
    MIDPOINT = "midpoint"
    COVERAGE = "coverage"
    CLAUSE_BOUNDARY = "clause_boundary"
    ACTION_BOUNDARY = "action_boundary"
    CAMERA_CHANGE = "camera_change"
    TRANSITION_BOUNDARY = "transition_boundary"
    ACTION_WINDOW = "action_window"
    HIGH_MOTION = "high_motion"
    LOW_MOTION = "low_motion"
    DETERMINISTIC_WARNING = "deterministic_warning"
    FACE_TRACK = "face_track"
    OCR = "ocr"


class VisualQAAttemptType(StrEnum):
    FIRST_PASS = "first_pass"
    ADJUDICATION = "adjudication"


class VisualQAEvidenceType(StrEnum):
    SAMPLE_FRAME = "sample_frame"
    REFERENCE_COMPARISON = "reference_comparison"
    DETERMINISTIC_MEASUREMENT = "deterministic_measurement"
    WHOLE_FILE = "whole_file"


class VisualQAFailureCode(StrEnum):
    """Non-retryable structural failures raised before any paid request."""

    PROJECT_NOT_FOUND = "project_not_found"
    STORYBOARD_NOT_SELECTED = "storyboard_not_selected"
    STALE_STORYBOARD_VERSION = "stale_storyboard_version"
    SHOT_NOT_FOUND = "shot_not_found"
    CROSS_PROJECT_ASSET = "cross_project_asset"
    STALE_GENERATION_ATTEMPT = "stale_generation_attempt"
    UNSELECTED_GENERATION_ATTEMPT = "unselected_generation_attempt"
    MISSING_KEYFRAME = "missing_keyframe"
    MISSING_CANONICAL_VIDEO = "missing_canonical_video"
    INCOMPATIBLE_REFERENCE_VERSION = "incompatible_reference_version"
    MISSING_STATE_SNAPSHOT = "missing_state_snapshot"
    MISSING_REFERENCE_BUNDLE = "missing_reference_bundle"
    ASSET_HASH_MISMATCH = "asset_hash_mismatch"
    INCOMPLETE_SHOT_WORKFLOW = "incomplete_shot_workflow"
    MIXED_LINEAGE = "mixed_lineage"
    UNSUPPORTED_MEDIA = "unsupported_media"
    MISSING_QA_CONFIGURATION = "missing_qa_configuration"
    IDENTITY_CONFLICT = "identity_conflict"
    UNVALIDATED_PROVIDER_OUTPUT = "unvalidated_provider_output"


class VisualQAFailure(StrictContract):
    """A structured, non-retryable T20 lineage or configuration failure."""

    schema_version: Literal["1.0"] = "1.0"
    code: VisualQAFailureCode
    message: str = Field(min_length=1, max_length=500)
    retryable: bool = False
    reference_id: UUID | None = None


class VisualQATarget(StrictContract):
    """Exactly one evaluated asset with its authoritative lineage."""

    schema_version: Literal["1.0"] = "1.0"
    project_id: UUID
    storyboard_run_id: UUID
    storyboard_shot_id: UUID
    shot_sequence: int = Field(ge=0)
    target_type: VisualQATargetType
    target_asset_id: UUID
    target_asset_sha256: Sha256
    media_type: str = Field(min_length=1, max_length=128)
    shot_workflow_identity: Sha256
    canonical_shot_hash: Sha256
    shot_reference_bundle_hash: Sha256
    importance: VisualQAShotImportance
    usable_duration_us: Microseconds
    requested_generation_duration_us: Microseconds
    character_identity_version_ids: list[UUID] = Field(default_factory=list, max_length=32)
    character_reference_asset_ids: list[UUID] = Field(default_factory=list, max_length=64)
    location_identity_version_id: UUID | None = None
    location_reference_asset_ids: list[UUID] = Field(default_factory=list, max_length=32)
    character_state_snapshot_hashes: list[Sha256] = Field(default_factory=list, max_length=32)
    location_state_snapshot_hash: Sha256 | None = None
    required_props: list[str] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def keyframes_have_no_duration_envelope(self) -> VisualQATarget:
        if self.target_type is VisualQATargetType.VIDEO and self.usable_duration_us <= 0:
            raise ValueError("a video QA target requires a positive usable duration")
        return self


class VisualQASample(StrictContract):
    """One deterministically selected, decoded and hashed frame."""

    schema_version: Literal["1.0"] = "1.0"
    sample_id: UUID
    sequence: int = Field(ge=0)
    sample_type: VisualQASampleType
    requested_timestamp_us: Microseconds
    actual_timestamp_us: Microseconds
    shot_relative_timestamp_us: Microseconds
    frame_asset_id: UUID | None = None
    frame_sha256: Sha256
    source_asset_id: UUID
    selection_reason: str = Field(min_length=1, max_length=255)
    contact_sheet_position: int | None = Field(default=None, ge=0)
    measurements: dict[str, float] = Field(default_factory=dict)

    @field_validator("measurements")
    @classmethod
    def measurements_are_finite(cls, value: dict[str, float]) -> dict[str, float]:
        for name, measurement in value.items():
            if measurement != measurement or measurement in {float("inf"), float("-inf")}:
                raise ValueError(f"measurement {name} is not finite")
        return value


class VisualQASamplingManifest(StrictContract):
    """The complete, ordered, deduplicated sample plan bound into QA identity."""

    schema_version: Literal["1.0"] = "1.0"
    sampling_version: str = Field(min_length=1, max_length=64)
    target_type: VisualQATargetType
    source_asset_id: UUID
    measured_duration_us: Microseconds
    # A whole-file deterministic failure (a clip that will not decode) legitimately
    # has no samples; every other manifest carries at least one.
    samples: list[VisualQASample] = Field(default_factory=list, max_length=64)
    contact_sheet_asset_id: UUID | None = None
    contact_sheet_columns: int = Field(default=4, ge=1, le=8)

    @model_validator(mode="after")
    def samples_are_dense_unique_and_ordered(self) -> VisualQASamplingManifest:
        expected = list(range(len(self.samples)))
        if [sample.sequence for sample in self.samples] != expected:
            raise ValueError("sample sequences must be dense and ascending from zero")
        timestamps = [sample.actual_timestamp_us for sample in self.samples]
        if timestamps != sorted(timestamps):
            raise ValueError("samples must preserve canonical chronological ordering")
        if len(set(timestamps)) != len(timestamps):
            raise ValueError("sample timestamps must be unique")
        for sample in self.samples:
            if sample.actual_timestamp_us > self.measured_duration_us:
                raise ValueError("sample timestamps must be clamped to the measured duration")
        return self


class VisualQADeterministicMetric(StrictContract):
    """One measured deterministic check with its threshold and outcome."""

    schema_version: Literal["1.0"] = "1.0"
    code: str = Field(min_length=1, max_length=64)
    measurement: float | None = None
    threshold: float | None = None
    outcome: Literal["pass", "warning", "hard_failure", "not_applicable"]
    evidence_timestamp_us: Microseconds | None = None
    evidence_sample_id: UUID | None = None
    tool: str = Field(min_length=1, max_length=64)
    tool_version: str = Field(default="", max_length=128)
    diagnostic_code: str = Field(min_length=1, max_length=64)
    repair_code: VisualQARepairCode | None = None
    message: str = Field(default="", max_length=500)

    @field_validator("measurement", "threshold")
    @classmethod
    def finite(cls, value: float | None) -> float | None:
        if value is not None and (value != value or value in {float("inf"), float("-inf")}):
            raise ValueError("deterministic measurements must be finite")
        return value

    @model_validator(mode="after")
    def failures_carry_repair_codes(self) -> VisualQADeterministicMetric:
        if self.outcome == "hard_failure" and self.repair_code is None:
            raise ValueError("a deterministic hard failure must carry a repair code")
        return self


class VisualQADeterministicReport(StrictContract):
    """Everything measured before any paid visual-agent request."""

    schema_version: Literal["1.0"] = "1.0"
    check_version: str = Field(min_length=1, max_length=64)
    target_type: VisualQATargetType
    usable: bool
    measured_duration_us: Microseconds | None = None
    width: int | None = Field(default=None, gt=0)
    height: int | None = Field(default=None, gt=0)
    frame_rate: str = Field(default="", max_length=32)
    metrics: list[VisualQADeterministicMetric] = Field(default_factory=list, max_length=64)

    @property
    def hard_failures(self) -> list[VisualQADeterministicMetric]:
        return [metric for metric in self.metrics if metric.outcome == "hard_failure"]

    @property
    def warnings(self) -> list[VisualQADeterministicMetric]:
        return [metric for metric in self.metrics if metric.outcome == "warning"]

    @model_validator(mode="after")
    def usable_reports_have_no_hard_failure(self) -> VisualQADeterministicReport:
        if self.usable and any(metric.outcome == "hard_failure" for metric in self.metrics):
            raise ValueError("a usable deterministic report cannot contain a hard failure")
        return self


class VisualQABoundingBox(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    width: float = Field(gt=0, le=1)
    height: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def inside_frame(self) -> VisualQABoundingBox:
        if self.x + self.width > 1.000001 or self.y + self.height > 1.000001:
            raise ValueError("a bounding box must stay inside the normalized frame")
        return self


class VisualQAEvidence(StrictContract):
    """A concrete, replayable pointer to what a finding is based on."""

    schema_version: Literal["1.0"] = "1.0"
    evidence_id: UUID
    evidence_type: VisualQAEvidenceType
    sample_id: UUID | None = None
    frame_asset_id: UUID | None = None
    source_asset_id: UUID
    source_relative_timestamp_us: Microseconds | None = None
    shot_relative_timestamp_us: Microseconds | None = None
    contact_sheet_position: int | None = Field(default=None, ge=0)
    bounding_box: VisualQABoundingBox | None = None
    entity_kind: Literal["character", "location", "prop", "state", "none"] = "none"
    entity_id: UUID | None = None
    prop_reference: str | None = Field(default=None, max_length=128)
    compared_reference_asset_id: UUID | None = None
    measurement: float | None = None
    confidence: Confidence = 1.0
    explanation: str = Field(default="", max_length=500)

    @field_validator("measurement")
    @classmethod
    def finite(cls, value: float | None) -> float | None:
        if value is not None and (value != value or value in {float("inf"), float("-inf")}):
            raise ValueError("evidence measurements must be finite")
        return value

    @model_validator(mode="after")
    def frame_evidence_locates_a_frame(self) -> VisualQAEvidence:
        if self.evidence_type in {
            VisualQAEvidenceType.SAMPLE_FRAME,
            VisualQAEvidenceType.REFERENCE_COMPARISON,
        } and (self.sample_id is None or self.source_relative_timestamp_us is None):
            raise ValueError("frame evidence requires a sample ID and an exact timestamp")
        return self


class VisualQAFinding(StrictContract):
    """One actionable observation with its evidence and structured correction."""

    schema_version: Literal["1.0"] = "1.0"
    finding_id: UUID
    dimension: VisualQADimension
    severity: Literal["info", "warning", "hard_failure"]
    code: str = Field(min_length=1, max_length=64)
    summary: str = Field(min_length=1, max_length=500)
    proposed_correction: str = Field(default="", max_length=500)
    repair_codes: list[VisualQARepairCode] = Field(default_factory=list, max_length=8)
    confidence: Confidence
    evidence: list[VisualQAEvidence] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def actionable_findings_cite_evidence(self) -> VisualQAFinding:
        if self.severity == "info":
            return self
        if not self.evidence:
            raise ValueError("a warning or hard failure must cite at least one evidence item")
        if self.severity == "hard_failure":
            if not self.repair_codes:
                raise ValueError("a hard failure must carry at least one repair code")
            whole_file = any(
                item.evidence_type is VisualQAEvidenceType.WHOLE_FILE for item in self.evidence
            )
            located = any(item.sample_id is not None for item in self.evidence)
            if not (whole_file or located):
                raise ValueError(
                    "a hard failure requires a located frame or a whole-file deterministic failure"
                )
        return self


class VisualQADimensionResult(StrictContract):
    """One rubric dimension as validated by application code."""

    schema_version: Literal["1.0"] = "1.0"
    dimension: VisualQADimension
    applicable: bool = True
    raw_score: RawScore
    weight: Weight
    effective_weight: Weight
    weighted_contribution: float = Field(ge=0, le=100)
    confidence: Confidence
    findings: list[VisualQAFinding] = Field(default_factory=list, max_length=16)
    warning_codes: list[str] = Field(default_factory=list, max_length=16)
    hard_failure_codes: list[str] = Field(default_factory=list, max_length=16)
    repair_codes: list[VisualQARepairCode] = Field(default_factory=list, max_length=8)
    evaluator: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=128)
    rubric_version: str = Field(min_length=1, max_length=64)

    @property
    def evidence(self) -> list[VisualQAEvidence]:
        return [item for finding in self.findings for item in finding.evidence]

    @model_validator(mode="after")
    def contribution_is_recomputed(self) -> VisualQADimensionResult:
        if not self.applicable:
            if self.effective_weight or self.weighted_contribution:
                raise ValueError("a non-applicable dimension contributes nothing")
            return self
        expected = self.raw_score * self.effective_weight / 100
        if abs(expected - self.weighted_contribution) > 1e-6:
            raise ValueError("weighted_contribution must equal raw_score * effective_weight / 100")
        if self.hard_failure_codes and not self.repair_codes:
            raise ValueError("a dimension hard failure must carry a repair code")
        return self


class VisualQARubricDimension(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    dimension: VisualQADimension
    weight: Weight


class VisualQARubric(StrictContract):
    """The authoritative weighted rubric. Total weight is exactly 100."""

    schema_version: Literal["1.0"] = "1.0"
    rubric_version: str = Field(min_length=1, max_length=64)
    dimensions: list[VisualQARubricDimension] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def weights_total_one_hundred(self) -> VisualQARubric:
        names = [item.dimension for item in self.dimensions]
        if len(set(names)) != len(names):
            raise ValueError("each rubric dimension appears exactly once")
        total = sum(item.weight for item in self.dimensions)
        if abs(total - 100) > 1e-9:
            raise ValueError("total rubric weight must equal 100")
        return self

    def weight_for(self, dimension: VisualQADimension) -> float:
        for item in self.dimensions:
            if item.dimension is dimension:
                return item.weight
        raise KeyError(dimension)


class VisualQAThresholds(StrictContract):
    """Versioned pass thresholds and the bounded adjudication policy."""

    schema_version: Literal["1.0"] = "1.0"
    threshold_version: str = Field(min_length=1, max_length=64)
    utility_pass_score: RawScore = 85
    normal_pass_score: RawScore = 85
    hero_pass_score: RawScore = 90
    targeted_repair_floor: RawScore = 75
    adjudication_confidence_floor: Confidence = 0.70
    adjudication_decision_confidence: Confidence = 0.80
    near_threshold_margin: RawScore = 2.0
    max_adjudication_attempts: int = Field(default=1, ge=0, le=3)

    def pass_score(self, importance: VisualQAShotImportance) -> float:
        return {
            VisualQAShotImportance.UTILITY: self.utility_pass_score,
            VisualQAShotImportance.NORMAL: self.normal_pass_score,
            VisualQAShotImportance.HERO: self.hero_pass_score,
        }[importance]


class VisualQASampleReference(StrictContract):
    """A bounded pointer handed to the visual agent; bytes are fetched by the adapter."""

    schema_version: Literal["1.0"] = "1.0"
    sample_id: UUID
    sequence: int = Field(ge=0)
    sample_type: VisualQASampleType
    shot_relative_timestamp_us: Microseconds
    source_relative_timestamp_us: Microseconds
    frame_sha256: Sha256


class VisualQAReferenceDescriptor(StrictContract):
    """One approved T19 reference offered to the visual agent for comparison."""

    schema_version: Literal["1.0"] = "1.0"
    asset_id: UUID
    sha256: Sha256
    role: Literal[
        "character_identity", "character_state", "location_identity", "location_state", "prop"
    ]
    entity_id: UUID
    identity_version_id: UUID | None = None
    label: str = Field(default="", max_length=128)


class VisualQAProviderRequest(StrictContract):
    """Bounded, provider-neutral shot intent. Never a whole episode or project."""

    schema_version: Literal["1.0"] = "1.0"
    contract_version: Literal["visual-qa/1.0"] = "visual-qa/1.0"
    qa_attempt_identity: Sha256
    attempt_number: int = Field(ge=1)
    attempt_type: VisualQAAttemptType
    project_id: UUID
    storyboard_shot_id: UUID
    target_type: VisualQATargetType
    storyboard_objective: str = Field(min_length=1, max_length=2048)
    required_character_ids: list[UUID] = Field(default_factory=list, max_length=16)
    required_character_count: int = Field(ge=0, le=16)
    required_location_id: UUID | None = None
    character_state_summaries: dict[str, str] = Field(default_factory=dict)
    location_state_summary: str = Field(default="", max_length=1024)
    required_action: str = Field(default="", max_length=1024)
    secondary_action: str = Field(default="", max_length=1024)
    camera_framing: str = Field(default="", max_length=64)
    camera_angle: str = Field(default="", max_length=64)
    camera_movement: str = Field(default="", max_length=64)
    composition_requirements: list[str] = Field(default_factory=list, max_length=16)
    required_props: list[str] = Field(default_factory=list, max_length=16)
    incoming_continuity_summary: str = Field(default="", max_length=2048)
    outgoing_continuity_summary: str = Field(default="", max_length=2048)
    samples: list[VisualQASampleReference] = Field(min_length=1, max_length=64)
    contact_sheet_asset_id: UUID | None = None
    references: list[VisualQAReferenceDescriptor] = Field(default_factory=list, max_length=32)
    deterministic_summary: list[str] = Field(default_factory=list, max_length=32)
    rubric_version: str = Field(min_length=1, max_length=64)
    threshold_version: str = Field(min_length=1, max_length=64)
    prompt_version: str = Field(min_length=1, max_length=64)
    trace_context: dict[str, str] = Field(default_factory=dict)


class VisualQAProviderDimensionScore(StrictContract):
    """A proposal. Application code, not the provider, owns the canonical score."""

    schema_version: Literal["1.0"] = "1.0"
    dimension: VisualQADimension
    raw_score: RawScore
    confidence: Confidence
    applicable: bool = True
    summary: str = Field(default="", max_length=500)


class VisualQAProviderFinding(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    dimension: VisualQADimension
    severity: Literal["info", "warning", "hard_failure"]
    code: str = Field(min_length=1, max_length=64)
    summary: str = Field(min_length=1, max_length=500)
    proposed_correction: str = Field(default="", max_length=500)
    repair_codes: list[VisualQARepairCode] = Field(default_factory=list, max_length=8)
    confidence: Confidence
    sample_ids: list[UUID] = Field(default_factory=list, max_length=16)
    bounding_box: VisualQABoundingBox | None = None
    compared_reference_asset_id: UUID | None = None


class VisualQAProviderResult(StrictContract):
    """Exactly what a visual agent may return. No overall score is accepted."""

    schema_version: Literal["1.0"] = "1.0"
    contract_version: Literal["visual-qa/1.0"] = "visual-qa/1.0"
    qa_attempt_identity: Sha256
    attempt_type: VisualQAAttemptType
    dimension_scores: list[VisualQAProviderDimensionScore] = Field(min_length=1, max_length=16)
    findings: list[VisualQAProviderFinding] = Field(default_factory=list, max_length=64)
    proposed_hard_failure_codes: list[str] = Field(default_factory=list, max_length=16)
    repair_codes: list[VisualQARepairCode] = Field(default_factory=list, max_length=16)
    warning_codes: list[str] = Field(default_factory=list, max_length=16)
    overall_confidence: Confidence
    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=128)
    provider_request_id: str | None = Field(default=None, max_length=255)
    usage: dict[str, float] = Field(default_factory=dict)
    redacted_metadata: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def dimensions_are_unique(self) -> VisualQAProviderResult:
        names = [item.dimension for item in self.dimension_scores]
        if len(set(names)) != len(names):
            raise ValueError("each dimension may be scored once per provider result")
        return self


class VisualQAAdjudication(StrictContract):
    """The bounded second opinion. Both the original and this result persist."""

    schema_version: Literal["1.0"] = "1.0"
    adjudication_id: UUID
    policy_version: str = Field(min_length=1, max_length=64)
    triggered_by: list[str] = Field(min_length=1, max_length=8)
    first_pass_provider: str = Field(min_length=1, max_length=64)
    first_pass_model: str = Field(min_length=1, max_length=128)
    adjudicator_provider: str = Field(min_length=1, max_length=64)
    adjudicator_model: str = Field(min_length=1, max_length=128)
    adjudicator_confidence: Confidence
    decided: bool
    disagreement_summary: list[str] = Field(default_factory=list, max_length=16)
    resulting_outcome_hint: VisualQAOutcome
    attempts_used: int = Field(ge=1, le=3)


class VisualQARepairRecommendation(StrictContract):
    """What T21 should consider. T20 never executes any of it."""

    schema_version: Literal["1.0"] = "1.0"
    routing: VisualQARoutingRecommendation
    repair_codes: list[VisualQARepairCode] = Field(default_factory=list, max_length=16)
    rationale: str = Field(default="", max_length=500)
    executed: Literal[False] = False


class VisualQAScore(StrictContract):
    """The canonical recomputed score. Never a provider-supplied total."""

    schema_version: Literal["1.0"] = "1.0"
    rubric_version: str = Field(min_length=1, max_length=64)
    threshold_version: str = Field(min_length=1, max_length=64)
    importance: VisualQAShotImportance
    pass_threshold: RawScore
    total: RawScore
    applied_weight_total: Weight
    dimensions: list[VisualQADimensionResult] = Field(min_length=1, max_length=16)
    confidence: Confidence

    @model_validator(mode="after")
    def total_is_the_sum_of_contributions(self) -> VisualQAScore:
        applicable = [item for item in self.dimensions if item.applicable]
        if not applicable:
            raise ValueError("at least one rubric dimension must be applicable")
        weight_total = sum(item.effective_weight for item in applicable)
        if abs(weight_total - self.applied_weight_total) > 1e-6:
            raise ValueError("applied_weight_total must equal the applicable effective weights")
        if abs(weight_total - 100) > 1e-6:
            raise ValueError("applicable effective weights must be redistributed to total 100")
        expected = sum(item.weighted_contribution for item in applicable)
        if abs(expected - self.total) > 1e-6:
            raise ValueError("total must equal the sum of weighted dimension contributions")
        return self


class VisualQAResult(StrictContract):
    """The canonical, persisted outcome of one QA run."""

    schema_version: Literal["1.0"] = "1.0"
    contract_version: Literal["visual-qa/1.0"] = "visual-qa/1.0"
    qa_run_id: UUID
    qa_identity: Sha256
    input_hash: Sha256
    target: VisualQATarget
    outcome: VisualQAOutcome
    score: VisualQAScore
    hard_failure: bool
    hard_failure_codes: list[str] = Field(default_factory=list, max_length=32)
    warning_codes: list[str] = Field(default_factory=list, max_length=32)
    repair_codes: list[VisualQARepairCode] = Field(default_factory=list, max_length=16)
    recommendation: VisualQARepairRecommendation
    deterministic_report: VisualQADeterministicReport
    sampling_manifest: VisualQASamplingManifest
    adjudication: VisualQAAdjudication | None = None
    human_review_decision: Literal["approved", "rejected"] | None = None
    human_reviewer: str | None = Field(default=None, max_length=255)
    first_pass_provider: str = Field(min_length=1, max_length=64)
    first_pass_model: str = Field(min_length=1, max_length=128)
    pipeline_version: str = Field(min_length=1, max_length=64)
    cost_microusd: int = Field(default=0, ge=0)
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("visual QA timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def outcome_is_consistent(self) -> VisualQAResult:
        if self.hard_failure != bool(self.hard_failure_codes):
            raise ValueError("hard_failure must agree with hard_failure_codes")
        if self.hard_failure and self.outcome is not VisualQAOutcome.FAIL:
            raise ValueError("any hard failure forces the canonical outcome to FAIL")
        if self.outcome is not VisualQAOutcome.PASS and not self.repair_codes:
            raise ValueError("a failed or review-required result requires repair codes")
        if self.outcome is VisualQAOutcome.PASS and (
            self.score.total < self.score.pass_threshold or self.hard_failure
        ):
            raise ValueError("PASS requires the pass threshold and no hard failure")
        if self.recommendation.executed:
            raise ValueError("T20 never executes a repair recommendation")
        return self


__all__ = [
    "CONTRACT_VERSION",
    "VisualQAAdjudication",
    "VisualQAAttemptType",
    "VisualQABoundingBox",
    "VisualQADeterministicMetric",
    "VisualQADeterministicReport",
    "VisualQADimension",
    "VisualQADimensionResult",
    "VisualQAEvidence",
    "VisualQAEvidenceType",
    "VisualQAFailure",
    "VisualQAFailureCode",
    "VisualQAFinding",
    "VisualQAOutcome",
    "VisualQAProviderDimensionScore",
    "VisualQAProviderFinding",
    "VisualQAProviderRequest",
    "VisualQAProviderResult",
    "VisualQAReferenceDescriptor",
    "VisualQARepairCode",
    "VisualQARepairRecommendation",
    "VisualQAResult",
    "VisualQARoutingRecommendation",
    "VisualQARubric",
    "VisualQARubricDimension",
    "VisualQASample",
    "VisualQASampleReference",
    "VisualQASampleType",
    "VisualQASamplingManifest",
    "VisualQAScore",
    "VisualQAShotImportance",
    "VisualQATarget",
    "VisualQATargetType",
    "VisualQAThresholds",
]
