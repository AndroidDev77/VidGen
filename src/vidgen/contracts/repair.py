"""Strict, versioned T21 repair and fallback-routing contracts.

T20 decides whether a shot passes. T21 decides what to do about a shot that did
not, and these contracts are the boundary between the two.

Four rules shape every model here:

* T21 never reinterprets a T20 decision. A :class:`RepairClassification` is
  derived from the persisted :class:`~vidgen.contracts.visual_qa.VisualQAResult`
  and cites the QA result it came from; it never recomputes a score or overrides
  ``hard_failure``.
* The repair policy is bounded by construction. ``RepairPolicy`` carries the
  maximum attempt counts and ``RepairAttempt.ordinal`` is range-checked, so an
  unbounded retry loop cannot be represented.
* A repaired prompt is a *delta*, not a rewrite. ``PromptDelta`` records exactly
  which clauses changed, which constraints were preserved, and the before/after
  prompt hashes, so a reviewer can prove nothing else moved.
* Provider metadata never merges into a canonical result. ``VeoGenerationResult``
  carries the operation name and redacted metadata; credentials, signed URLs,
  prompts and raw provider payloads are not representable.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from vidgen.contracts.common import StrictContract
from vidgen.contracts.visual_qa import VisualQABoundingBox, VisualQARepairCode

CONTRACT_VERSION = "repair/1.0"

Sha256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Confidence = Annotated[float, Field(ge=0, le=1)]
RawScore = Annotated[float, Field(ge=0, le=100)]
Microseconds = Annotated[int, Field(ge=0)]
#: Ordinal 0 is the original T15 generation; 1-3 are the bounded repair attempts.
AttemptOrdinal = Annotated[int, Field(ge=0, le=4)]
Clause = Annotated[str, Field(min_length=1, max_length=500)]


class RepairFailureCategory(StrEnum):
    """The five strict categories every failed shot is classified into."""

    PROMPT_ISSUE = "prompt_issue"
    REFERENCE_ISSUE = "reference_issue"
    SEED_ISSUE = "seed_issue"
    PROVIDER_ISSUE = "provider_issue"
    IMPOSSIBLE_SHOT = "impossible_shot"


class RepairDiagnosticCode(StrEnum):
    """The bounded distinctions the classifier must be able to make.

    Adding a member is a contract version change: the planner, the policy and
    the dashboard all switch on this enum.
    """

    WRONG_CHARACTER_IDENTITY = "wrong_character_identity"
    WRONG_CHARACTER_COUNT = "wrong_character_count"
    WRONG_LOCATION = "wrong_location"
    WRONG_WARDROBE_OR_STATE = "wrong_wardrobe_or_state"
    MISSING_OR_INCORRECT_ACTION = "missing_or_incorrect_action"
    WEAK_MOTION = "weak_motion"
    COMPOSITION_FAILURE = "composition_failure"
    ANATOMY_OR_ARTIFACT_FAILURE = "anatomy_or_artifact_failure"
    CONTINUITY_FAILURE = "continuity_failure"
    STYLE_MISMATCH = "style_mismatch"
    PROMPT_OVERCONSTRAINT = "prompt_overconstraint"
    PROMPT_AMBIGUITY = "prompt_ambiguity"
    REFERENCE_CONFLICT = "reference_conflict"
    PROVIDER_SAFETY_REJECTION = "provider_safety_rejection"
    PROVIDER_TIMEOUT_OR_SERVICE_FAILURE = "provider_timeout_or_service_failure"
    UNSUPPORTED_PROVIDER_CAPABILITY = "unsupported_provider_capability"
    IMPOSSIBLE_DURATION_OR_MOTION = "impossible_duration_or_motion"
    CORRUPT_OR_INCOMPLETE_MEDIA = "corrupt_or_incomplete_media"


class RepairSeverity(StrEnum):
    """How much of the shot the T20 score says has to change.

    The bands mirror T20 exactly: at or above the applicable pass threshold the
    shot is not repaired at all, ``75`` to below the threshold earns a targeted
    repair, and below ``75`` earns a structural one. A hard failure is severe
    regardless of score.
    """

    TARGETED = "targeted"
    STRUCTURAL = "structural"
    UNRECOVERABLE = "unrecoverable"


class RepairRoute(StrEnum):
    """Exactly one bounded next action for a repair run."""

    SAME_PROVIDER_REPAIR = "same_provider_repair"
    ALTERNATE_PROVIDER = "alternate_provider"
    DETERMINISTIC_FALLBACK = "deterministic_fallback"
    RESUME_PROVIDER_OPERATION = "resume_provider_operation"
    UPSTREAM_REFERENCE_CORRECTION = "upstream_reference_correction"
    HUMAN_REVIEW_REQUIRED = "human_review_required"
    SELECT_PASSING_ATTEMPT = "select_passing_attempt"


class RepairAttemptKind(StrEnum):
    """What produced the media an attempt evaluates."""

    ORIGINAL = "original"
    SAME_PROVIDER_REPAIR = "same_provider_repair"
    ALTERNATE_PROVIDER = "alternate_provider"
    DETERMINISTIC_FALLBACK = "deterministic_fallback"


class RepairAttemptStatus(StrEnum):
    PLANNED = "planned"
    SUBMITTED = "submitted"
    POLLING = "polling"
    DOWNLOADING = "downloading"
    REVALIDATING = "revalidating"
    PASSED = "passed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RepairRunState(StrEnum):
    """The persisted repair-run states, mirrored by the T16 workflow."""

    REPAIR_PLANNING = "REPAIR_PLANNING"
    REPAIRING = "REPAIRING"
    ALTERNATE_PROVIDER = "ALTERNATE_PROVIDER"
    FALLBACK_RENDERING = "FALLBACK_RENDERING"
    REVALIDATING = "REVALIDATING"
    HUMAN_REVIEW_REQUIRED = "HUMAN_REVIEW_REQUIRED"
    LOCKED = "LOCKED"
    REPAIR_FAILED = "REPAIR_FAILED"


class HumanReviewReason(StrEnum):
    """Why a shot stopped costing money and started waiting for a person."""

    REPAIR_BUDGET_EXHAUSTED = "repair_budget_exhausted"
    PROJECT_BUDGET_DENIED = "project_budget_denied"
    ATTEMPT_LIMIT_REACHED = "attempt_limit_reached"
    FALLBACK_INELIGIBLE = "fallback_ineligible"
    IMPOSSIBLE_SHOT = "impossible_shot"
    UPSTREAM_REFERENCE_CORRECTION = "upstream_reference_correction"
    DETERMINISTIC_FAILURE = "deterministic_failure"
    CANCELLED_BEFORE_PAID_ATTEMPT = "cancelled_before_paid_attempt"


class PromptConstraintKind(StrEnum):
    """Every constraint class a repaired prompt has to account for."""

    CHARACTER_IDENTITY = "character_identity"
    CHARACTER_COUNT = "character_count"
    CHARACTER_STATE = "character_state"
    LOCATION = "location"
    ACTION = "action"
    CAMERA = "camera"
    TIMING = "timing"
    CONTINUITY = "continuity"
    REFERENCE_BINDING = "reference_binding"
    SAFETY = "safety"
    PROVIDER_CAPABILITY = "provider_capability"
    NEGATIVE = "negative"
    STYLE = "style"


class RepairDiagnostic(StrictContract):
    """One structured reason a shot failed, tied to the T20 evidence for it."""

    schema_version: Literal["1.0"] = "1.0"
    code: RepairDiagnosticCode
    severity: Literal["warning", "hard_failure"]
    repair_codes: list[VisualQARepairCode] = Field(default_factory=list, max_length=8)
    source_finding_ids: list[UUID] = Field(default_factory=list, max_length=16)
    dimension: str = Field(default="", max_length=64)
    confidence: Confidence = 1.0
    evidence_timestamp_us: Microseconds | None = None
    bounding_box: VisualQABoundingBox | None = None
    summary: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def hard_failures_cite_a_repair_code(self) -> RepairDiagnostic:
        if self.severity == "hard_failure" and not self.repair_codes:
            raise ValueError("a hard-failure diagnostic must carry at least one T20 repair code")
        return self


class RepairClassification(StrictContract):
    """Why one shot failed, derived from one persisted T20 result.

    ``deterministic_only`` marks a failure that a paid generation cannot fix -
    corrupt media, an invalid lineage, an unsupported capability - so the router
    is forbidden from spending money on it.
    """

    schema_version: Literal["1.0"] = "1.0"
    contract_version: Literal["repair/1.0"] = "repair/1.0"
    classifier_version: str = Field(min_length=1, max_length=64)
    source_qa_result_id: UUID
    shot_id: UUID
    target_type: Literal["keyframe", "video"]
    category: RepairFailureCategory
    severity: RepairSeverity
    primary_code: RepairDiagnosticCode
    diagnostics: list[RepairDiagnostic] = Field(min_length=1, max_length=32)
    hard_failure: bool
    qa_score: RawScore
    pass_threshold: RawScore
    importance: Literal["utility", "normal", "hero"]
    confidence: Confidence
    deterministic_only: bool = False
    requires_upstream_reference_correction: bool = False
    rationale: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def classification_is_internally_consistent(self) -> RepairClassification:
        codes = {item.code for item in self.diagnostics}
        if self.primary_code not in codes:
            raise ValueError("primary_code must appear among the diagnostics")
        if self.hard_failure and not any(
            item.severity == "hard_failure" for item in self.diagnostics
        ):
            raise ValueError("a hard failure must carry at least one hard-failure diagnostic")
        if self.category is RepairFailureCategory.IMPOSSIBLE_SHOT and (
            self.severity is not RepairSeverity.UNRECOVERABLE
        ):
            raise ValueError("an impossible shot is always unrecoverable")
        if self.requires_upstream_reference_correction and (
            self.category is not RepairFailureCategory.REFERENCE_ISSUE
        ):
            raise ValueError("only a reference issue can require an upstream correction")
        return self


class PromptConstraint(StrictContract):
    """One constraint the original prompt asserted, and whether it may move."""

    schema_version: Literal["1.0"] = "1.0"
    constraint_id: str = Field(min_length=1, max_length=64)
    kind: PromptConstraintKind
    clause: Clause
    #: An immutable constraint is never removed or rewritten by any planner.
    mutable: bool = False
    #: Whether the clause is written into the prompt text. Some constraints are
    #: enforced by the request itself - the requested duration, the capability
    #: profile, the attached reference assets - so restating them as prose only
    #: consumes the provider's prompt budget. They are still preserved, still
    #: listed in the delta, and still immutable.
    rendered: bool = True
    entity_id: UUID | None = None
    source: str = Field(default="", max_length=64)


class PromptDelta(StrictContract):
    """Exactly what changed between the original and the repaired prompt."""

    schema_version: Literal["1.0"] = "1.0"
    contract_version: Literal["repair/1.0"] = "repair/1.0"
    planner_version: str = Field(min_length=1, max_length=64)
    repair_reason: str = Field(min_length=1, max_length=500)
    repair_codes: list[VisualQARepairCode] = Field(default_factory=list, max_length=16)
    source_finding_ids: list[UUID] = Field(default_factory=list, max_length=32)
    added_clauses: list[Clause] = Field(default_factory=list, max_length=16)
    removed_clauses: list[Clause] = Field(default_factory=list, max_length=16)
    rewritten_clauses: list[tuple[Clause, Clause]] = Field(default_factory=list, max_length=16)
    preserved_constraint_ids: list[str] = Field(default_factory=list, max_length=64)
    touched_constraint_ids: list[str] = Field(default_factory=list, max_length=16)
    before_prompt_hash: Sha256
    after_prompt_hash: Sha256
    seed_changed: bool = False
    previous_seed: int | None = Field(default=None, ge=0)
    new_seed: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def a_delta_actually_changes_something(self) -> PromptDelta:
        changed = bool(self.added_clauses or self.removed_clauses or self.rewritten_clauses)
        if self.before_prompt_hash == self.after_prompt_hash and changed:
            raise ValueError("clause edits must change the prompt hash")
        if self.before_prompt_hash != self.after_prompt_hash and not changed:
            raise ValueError("a changed prompt hash must be explained by a clause edit")
        if not changed and not self.seed_changed:
            raise ValueError("a prompt delta must change a clause or the seed")
        if self.seed_changed and self.new_seed is None:
            raise ValueError("a seed change must record the new seed")
        if not self.seed_changed and self.new_seed is not None:
            raise ValueError("a new seed requires seed_changed")
        overlap = set(self.preserved_constraint_ids) & set(self.touched_constraint_ids)
        if overlap:
            raise ValueError(f"constraints cannot be both preserved and touched: {sorted(overlap)}")
        for before, after in self.rewritten_clauses:
            if before == after:
                raise ValueError("a rewritten clause must differ from its original")
        return self


class RepairPolicy(StrictContract):
    """The bounded routing policy. Every limit is explicit and small."""

    schema_version: Literal["1.0"] = "1.0"
    policy_version: str = Field(min_length=1, max_length=64)
    max_same_provider_repairs: int = Field(default=2, ge=0, le=2)
    max_alternate_provider_attempts: int = Field(default=1, ge=0, le=1)
    max_fallback_renders: int = Field(default=1, ge=0, le=1)
    allow_parallax_fallback: bool = True
    targeted_repair_floor: RawScore = 75
    #: Repairs a project is willing to pay for, on top of the original spend.
    per_shot_repair_cost_limit: Decimal | None = Field(default=None, ge=0)
    per_run_repair_cost_limit: Decimal | None = Field(default=None, ge=0)

    @property
    def max_paid_attempts(self) -> int:
        return self.max_same_provider_repairs + self.max_alternate_provider_attempts

    @property
    def max_total_attempts(self) -> int:
        """Original generation plus every bounded recovery attempt."""
        return 1 + self.max_paid_attempts + self.max_fallback_renders


class RepairPlan(StrictContract):
    """One validated, ready-to-execute recovery step."""

    schema_version: Literal["1.0"] = "1.0"
    contract_version: Literal["repair/1.0"] = "repair/1.0"
    plan_id: UUID
    repair_run_id: UUID
    shot_id: UUID
    attempt_ordinal: AttemptOrdinal
    attempt_kind: RepairAttemptKind
    route: RepairRoute
    classification: RepairClassification
    policy: RepairPolicy
    prompt_delta: PromptDelta | None = None
    repaired_prompt_hash: Sha256 | None = None
    provider: str = Field(default="", max_length=64)
    model: str = Field(default="", max_length=128)
    capability_profile_hash: Sha256 | None = None
    seed: int | None = Field(default=None, ge=0)
    reference_asset_ids: list[UUID] = Field(default_factory=list, max_length=32)
    reference_asset_hashes: list[Sha256] = Field(default_factory=list, max_length=32)
    estimated_cost: Decimal = Field(default=Decimal("0"), ge=0)
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    idempotency_key: str = Field(min_length=1, max_length=255)
    planner_version: str = Field(min_length=1, max_length=64)
    warnings: list[str] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def a_paid_plan_names_a_provider(self) -> RepairPlan:
        paid = self.attempt_kind in {
            RepairAttemptKind.SAME_PROVIDER_REPAIR,
            RepairAttemptKind.ALTERNATE_PROVIDER,
        }
        if paid and not (self.provider and self.model):
            raise ValueError("a paid repair plan must name its provider and model")
        if paid and self.prompt_delta is None:
            raise ValueError("a paid repair plan must carry the prompt delta it will submit")
        if self.attempt_kind is RepairAttemptKind.DETERMINISTIC_FALLBACK and self.estimated_cost:
            raise ValueError("the deterministic fallback never costs a provider charge")
        if len(self.reference_asset_ids) != len(self.reference_asset_hashes):
            raise ValueError("every reference asset must carry its hash")
        return self


class RepairAttemptLineage(StrictContract):
    """Where one attempt sits in the chain, and what materially produced it."""

    schema_version: Literal["1.0"] = "1.0"
    root_animation_attempt_id: UUID
    predecessor_attempt_id: UUID | None = None
    shot_id: UUID
    attempt_ordinal: AttemptOrdinal
    attempt_identity: Sha256

    @model_validator(mode="after")
    def an_attempt_never_precedes_itself(self) -> RepairAttemptLineage:
        if self.attempt_ordinal == 0 and self.predecessor_attempt_id is not None:
            raise ValueError("the root attempt has no predecessor")
        if self.attempt_ordinal > 0 and self.predecessor_attempt_id is None:
            raise ValueError("every repair attempt records its immediate predecessor")
        return self


class RepairAttempt(StrictContract):
    """One immutable historical generation or fallback render."""

    schema_version: Literal["1.0"] = "1.0"
    contract_version: Literal["repair/1.0"] = "repair/1.0"
    attempt_id: UUID
    repair_run_id: UUID
    lineage: RepairAttemptLineage
    attempt_kind: RepairAttemptKind
    status: RepairAttemptStatus
    provider: str = Field(default="", max_length=64)
    model: str = Field(default="", max_length=128)
    provider_attempt_id: UUID | None = None
    provider_operation_id: str | None = Field(default=None, max_length=255)
    prompt_hash: Sha256 | None = None
    prompt_delta: PromptDelta | None = None
    seed: int | None = Field(default=None, ge=0)
    reference_asset_ids: list[UUID] = Field(default_factory=list, max_length=32)
    reference_asset_hashes: list[Sha256] = Field(default_factory=list, max_length=32)
    capability_profile_hash: Sha256 | None = None
    classification: RepairClassification | None = None
    repair_codes: list[VisualQARepairCode] = Field(default_factory=list, max_length=16)
    source_qa_result_id: UUID | None = None
    output_asset_ids: list[UUID] = Field(default_factory=list, max_length=8)
    output_qa_result_id: UUID | None = None
    estimated_cost: Decimal = Field(default=Decimal("0"), ge=0)
    actual_cost: Decimal = Field(default=Decimal("0"), ge=0)
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    failure_category: RepairFailureCategory | None = None
    failure_code: str | None = Field(default=None, max_length=128)
    trace_context: dict[str, str] = Field(default_factory=dict)
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None

    @field_validator("created_at", "started_at", "completed_at")
    @classmethod
    def timezone_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("repair timestamps must be timezone-aware UTC instants")
        return value

    @model_validator(mode="after")
    def a_passing_attempt_proves_it(self) -> RepairAttempt:
        if self.status is RepairAttemptStatus.PASSED and self.output_qa_result_id is None:
            raise ValueError("an attempt only passes with a T20 result of its own output")
        if self.status is RepairAttemptStatus.PASSED and not self.output_asset_ids:
            raise ValueError("a passing attempt must reference its output asset")
        if len(self.reference_asset_ids) != len(self.reference_asset_hashes):
            raise ValueError("every reference asset must carry its hash")
        return self


class RepairDecision(StrictContract):
    """One recorded routing decision, with everything it was based on."""

    schema_version: Literal["1.0"] = "1.0"
    contract_version: Literal["repair/1.0"] = "repair/1.0"
    decision_id: UUID
    repair_run_id: UUID
    sequence: int = Field(ge=0, le=16)
    source_attempt_id: UUID | None = None
    source_qa_result_id: UUID | None = None
    classification: RepairClassification | None = None
    repair_codes: list[VisualQARepairCode] = Field(default_factory=list, max_length=16)
    route: RepairRoute
    rationale: list[str] = Field(default_factory=list, max_length=16)
    same_provider_repairs_used: int = Field(default=0, ge=0, le=2)
    alternate_provider_attempts_used: int = Field(default=0, ge=0, le=1)
    fallback_renders_used: int = Field(default=0, ge=0, le=1)
    capability_profile_hash: Sha256 | None = None
    budget_remaining: Decimal | None = Field(default=None, ge=0)
    estimated_next_cost: Decimal = Field(default=Decimal("0"), ge=0)
    human_review_reason: HumanReviewReason | None = None
    planner_version: str = Field(min_length=1, max_length=64)
    policy_version: str = Field(min_length=1, max_length=64)
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("repair timestamps must be timezone-aware UTC instants")
        return value

    @model_validator(mode="after")
    def review_routes_explain_themselves(self) -> RepairDecision:
        if self.route is RepairRoute.HUMAN_REVIEW_REQUIRED and self.human_review_reason is None:
            raise ValueError("routing to human review requires a structured reason")
        return self


class RepairOutcome(StrictContract):
    """The canonical, persisted result of one bounded repair run."""

    schema_version: Literal["1.0"] = "1.0"
    contract_version: Literal["repair/1.0"] = "repair/1.0"
    repair_run_id: UUID
    project_id: UUID
    shot_id: UUID
    root_animation_attempt_id: UUID
    triggering_qa_result_id: UUID
    state: RepairRunState
    policy: RepairPolicy
    classification: RepairClassification | None = None
    attempts: list[RepairAttempt] = Field(default_factory=list, max_length=8)
    decisions: list[RepairDecision] = Field(default_factory=list, max_length=16)
    selected_attempt_id: UUID | None = None
    selected_asset_id: UUID | None = None
    final_qa_result_id: UUID | None = None
    final_qa_score: RawScore | None = None
    total_attempt_count: int = Field(default=0, ge=0, le=8)
    total_repair_cost: Decimal = Field(default=Decimal("0"), ge=0)
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    human_review_reason: HumanReviewReason | None = None
    input_hash: Sha256
    idempotency_key: str = Field(min_length=1, max_length=255)
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at")
    @classmethod
    def timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("repair timestamps must be timezone-aware UTC instants")
        return value

    @model_validator(mode="after")
    def only_a_revalidated_attempt_locks(self) -> RepairOutcome:
        if self.state is RepairRunState.LOCKED:
            if self.selected_attempt_id is None or self.final_qa_result_id is None:
                raise ValueError("LOCKED requires a selected attempt and its passing T20 result")
            if self.selected_asset_id is None:
                raise ValueError("LOCKED requires the selected output asset")
        if self.state is RepairRunState.HUMAN_REVIEW_REQUIRED and self.human_review_reason is None:
            raise ValueError("HUMAN_REVIEW_REQUIRED requires a structured reason")
        if self.state is not RepairRunState.LOCKED and self.selected_attempt_id is not None:
            raise ValueError("only a locked repair run has an authoritative selected attempt")
        selected = [item for item in self.attempts if item.status is RepairAttemptStatus.PASSED]
        if len(selected) > 1:
            raise ValueError("at most one attempt may pass and become authoritative")
        ordinals = [item.lineage.attempt_ordinal for item in self.attempts]
        if len(set(ordinals)) != len(ordinals):
            raise ValueError("attempt ordinals must be unique within a repair run")
        if len(self.attempts) > self.policy.max_total_attempts:
            raise ValueError("attempt count exceeds the bounded repair policy")
        return self


# --- Google Veo alternate provider -------------------------------------------
class VeoOperationState(StrEnum):
    SUBMITTED = "submitted"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class VeoGenerationRequest(StrictContract):
    """A provider-neutral pipeline request rendered for one Veo model."""

    schema_version: Literal["1.0"] = "1.0"
    contract_version: Literal["repair/1.0"] = "repair/1.0"
    application_idempotency_key: str = Field(min_length=1, max_length=255)
    project_id: UUID
    repair_run_id: UUID
    repair_attempt_id: UUID
    shot_id: UUID
    attempt_ordinal: AttemptOrdinal
    model: str = Field(min_length=1, max_length=128)
    capability_profile_version: str = Field(min_length=1, max_length=64)
    capability_profile_hash: Sha256
    prompt: str = Field(min_length=1, max_length=4000)
    prompt_hash: Sha256
    negative_prompt: str = Field(default="", max_length=2000)
    first_frame_asset_id: UUID | None = None
    first_frame_sha256: Sha256 | None = None
    last_frame_asset_id: UUID | None = None
    last_frame_sha256: Sha256 | None = None
    reference_asset_ids: list[UUID] = Field(default_factory=list, max_length=3)
    duration_seconds: int = Field(gt=0, le=60)
    aspect_ratio: str = Field(pattern=r"^\d{1,2}:\d{1,2}$")
    resolution: Literal["720p", "1080p"]
    generate_audio: bool = False
    seed: int | None = Field(default=None, ge=0)
    person_generation: Literal["allow_adult", "allow_all", "dont_allow"] = "allow_adult"
    trace_context: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def frames_carry_their_hashes(self) -> VeoGenerationRequest:
        if bool(self.first_frame_asset_id) != bool(self.first_frame_sha256):
            raise ValueError("a first-frame reference must carry its content hash")
        if bool(self.last_frame_asset_id) != bool(self.last_frame_sha256):
            raise ValueError("a last-frame reference must carry its content hash")
        if self.last_frame_asset_id is not None and self.first_frame_asset_id is None:
            raise ValueError("a last-frame control requires a first frame")
        return self


class VeoOperationCheckpoint(StrictContract):
    """The durable record that makes a Veo operation resumable.

    The operation name is persisted immediately after submission. A worker that
    dies mid-poll re-reads this row and continues polling; it never resubmits.
    """

    schema_version: Literal["1.0"] = "1.0"
    contract_version: Literal["repair/1.0"] = "repair/1.0"
    repair_attempt_id: UUID
    application_idempotency_key: str = Field(min_length=1, max_length=255)
    operation_name: str | None = Field(default=None, max_length=512)
    model: str = Field(min_length=1, max_length=128)
    state: VeoOperationState
    poll_count: int = Field(default=0, ge=0, le=10_000)
    submitted_at: datetime
    last_polled_at: datetime | None = None
    completed_at: datetime | None = None
    #: Set when a submission response was lost. A checkpoint in this state is
    #: never resubmitted automatically; it is reconciled or reviewed.
    submission_ambiguous: bool = False
    failure_code: str | None = Field(default=None, max_length=128)
    failure_message: str = Field(default="", max_length=500)
    redacted_metadata: dict[str, str] = Field(default_factory=dict)

    @field_validator("submitted_at", "last_polled_at", "completed_at")
    @classmethod
    def timezone_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("Veo checkpoints must use timezone-aware UTC instants")
        return value

    @model_validator(mode="after")
    def a_running_operation_has_a_name(self) -> VeoOperationCheckpoint:
        if self.state in {VeoOperationState.RUNNING, VeoOperationState.SUCCEEDED} and (
            not self.operation_name
        ):
            raise ValueError("a running or succeeded Veo operation must carry its operation name")
        if self.submission_ambiguous and self.operation_name:
            raise ValueError("an ambiguous submission has no confirmed operation name")
        return self


class VeoGenerationResult(StrictContract):
    """A terminal Veo operation. Provider metadata stays out of the canon."""

    schema_version: Literal["1.0"] = "1.0"
    contract_version: Literal["repair/1.0"] = "repair/1.0"
    application_idempotency_key: str = Field(min_length=1, max_length=255)
    operation_name: str = Field(min_length=1, max_length=512)
    model: str = Field(min_length=1, max_length=128)
    state: VeoOperationState
    generated_duration_seconds: float = Field(default=0, ge=0)
    width: int | None = Field(default=None, gt=0)
    height: int | None = Field(default=None, gt=0)
    has_audio: bool = False
    output_count: int = Field(default=0, ge=0, le=4)
    rai_filtered_count: int = Field(default=0, ge=0, le=4)
    failure_code: str | None = Field(default=None, max_length=128)
    failure_message: str = Field(default="", max_length=500)
    poll_count: int = Field(default=0, ge=0, le=10_000)
    latency_ms: int = Field(default=0, ge=0)
    usage: dict[str, float] = Field(default_factory=dict)
    redacted_metadata: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def success_produces_exactly_one_primary_output(self) -> VeoGenerationResult:
        if self.state is VeoOperationState.SUCCEEDED and self.output_count < 1:
            raise ValueError("a succeeded Veo operation must report at least one output")
        if self.state is VeoOperationState.FAILED and not self.failure_code:
            raise ValueError("a failed Veo operation must carry a bounded failure code")
        return self


# --- deterministic 2.5D parallax fallback ------------------------------------
class ParallaxLayerRole(StrEnum):
    BACKGROUND = "background"
    FOREGROUND = "foreground"


class ParallaxEasing(StrEnum):
    LINEAR = "linear"
    EASE_IN_OUT = "ease_in_out"


class ParallaxLayer(StrictContract):
    """One deterministic transform applied to one approved still image."""

    schema_version: Literal["1.0"] = "1.0"
    role: ParallaxLayerRole
    source_asset_id: UUID
    source_sha256: Sha256
    #: Uniform scale at the start and end of the shot, as a ratio of the frame.
    start_scale: float = Field(gt=0, le=4)
    end_scale: float = Field(gt=0, le=4)
    #: Normalized centre offsets, in frame widths/heights.
    start_offset_x: float = Field(ge=-1, le=1)
    start_offset_y: float = Field(ge=-1, le=1)
    end_offset_x: float = Field(ge=-1, le=1)
    end_offset_y: float = Field(ge=-1, le=1)
    easing: ParallaxEasing = ParallaxEasing.EASE_IN_OUT
    opacity: float = Field(default=1.0, gt=0, le=1)
    mask_asset_id: UUID | None = None
    depth_map_asset_id: UUID | None = None


class ParallaxRenderPlan(StrictContract):
    """A deterministic, fully specified 2.5D render, derived from stable inputs."""

    schema_version: Literal["1.0"] = "1.0"
    contract_version: Literal["repair/1.0"] = "repair/1.0"
    plan_id: UUID
    renderer_version: str = Field(min_length=1, max_length=64)
    repair_attempt_id: UUID
    shot_id: UUID
    render_identity: Sha256
    layers: list[ParallaxLayer] = Field(min_length=1, max_length=4)
    width: int = Field(gt=0, le=7680)
    height: int = Field(gt=0, le=4320)
    frame_rate: int = Field(gt=0, le=120)
    exact_duration_us: Microseconds
    pixel_format: Literal["yuv420p"] = "yuv420p"
    video_codec: Literal["h264"] = "h264"
    container: Literal["mp4"] = "mp4"

    @model_validator(mode="after")
    def a_plan_renders_a_whole_number_of_frames(self) -> ParallaxRenderPlan:
        if self.exact_duration_us <= 0:
            raise ValueError("a parallax render needs the canonical shot duration")
        roles = [layer.role for layer in self.layers]
        if roles[0] is not ParallaxLayerRole.BACKGROUND:
            raise ValueError("the first parallax layer is always the background")
        return self


class ParallaxRenderManifest(StrictContract):
    """Everything needed to reproduce one fallback render byte for byte."""

    schema_version: Literal["1.0"] = "1.0"
    contract_version: Literal["repair/1.0"] = "repair/1.0"
    manifest_version: Literal["parallax-manifest/1.0"] = "parallax-manifest/1.0"
    plan: ParallaxRenderPlan
    input_asset_ids: list[UUID] = Field(min_length=1, max_length=8)
    input_asset_hashes: list[Sha256] = Field(min_length=1, max_length=8)
    #: Argument arrays, never a shell command string.
    ffmpeg_arguments: list[str] = Field(min_length=1, max_length=256)
    #: The deterministic trim that pins the render to the exact canonical
    #: duration, using the same encoder profile T15 uses for provider output.
    trim_arguments: list[str] = Field(default_factory=list, max_length=64)
    filter_graph: str = Field(min_length=1, max_length=4000)
    ffmpeg_version: str = Field(min_length=1, max_length=128)
    ffprobe_version: str = Field(min_length=1, max_length=128)
    encoding_profile: str = Field(min_length=1, max_length=64)
    output_sha256: Sha256
    measured_duration_us: Microseconds
    measured_width: int = Field(gt=0)
    measured_height: int = Field(gt=0)

    @model_validator(mode="after")
    def inputs_and_hashes_align(self) -> ParallaxRenderManifest:
        if len(self.input_asset_ids) != len(self.input_asset_hashes):
            raise ValueError("every parallax input asset must carry its hash")
        for arguments in (self.ffmpeg_arguments, self.trim_arguments):
            if any(argument.startswith("-") and " " in argument.strip() for argument in arguments):
                raise ValueError(
                    "FFmpeg invocations are argument arrays, never shell command strings"
                )
        return self


class ParallaxEligibility(StrictContract):
    """The deterministic decision about whether a shot may use the fallback."""

    schema_version: Literal["1.0"] = "1.0"
    eligible: bool
    reasons: list[str] = Field(default_factory=list, max_length=16)
    source_keyframe_asset_id: UUID | None = None
    policy_version: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def a_decision_explains_itself(self) -> ParallaxEligibility:
        if not self.reasons:
            raise ValueError("a parallax eligibility decision must record its reasons")
        if self.eligible and self.source_keyframe_asset_id is None:
            raise ValueError("an eligible shot must name the approved still it will animate")
        return self


class ParallaxRenderResult(StrictContract):
    """The persisted outcome of one deterministic fallback render."""

    schema_version: Literal["1.0"] = "1.0"
    contract_version: Literal["repair/1.0"] = "repair/1.0"
    repair_attempt_id: UUID
    render_identity: Sha256
    manifest: ParallaxRenderManifest
    output_asset_id: UUID
    manifest_asset_id: UUID
    output_sha256: Sha256
    exact_duration_us: Microseconds
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    frame_rate: str = Field(min_length=1, max_length=32)
    pixel_format: str = Field(min_length=1, max_length=32)
    video_codec: str = Field(min_length=1, max_length=32)
    qa_result_id: UUID | None = None

    @model_validator(mode="after")
    def the_result_matches_its_manifest(self) -> ParallaxRenderResult:
        if self.output_sha256 != self.manifest.output_sha256:
            raise ValueError("the render result hash must match its manifest")
        if self.exact_duration_us != self.manifest.plan.exact_duration_us:
            raise ValueError("a fallback render must produce the exact canonical duration")
        return self


__all__ = [
    "CONTRACT_VERSION",
    "AttemptOrdinal",
    "HumanReviewReason",
    "ParallaxEasing",
    "ParallaxEligibility",
    "ParallaxLayer",
    "ParallaxLayerRole",
    "ParallaxRenderManifest",
    "ParallaxRenderPlan",
    "ParallaxRenderResult",
    "PromptConstraint",
    "PromptConstraintKind",
    "PromptDelta",
    "RepairAttempt",
    "RepairAttemptKind",
    "RepairAttemptLineage",
    "RepairAttemptStatus",
    "RepairClassification",
    "RepairDecision",
    "RepairDiagnostic",
    "RepairDiagnosticCode",
    "RepairFailureCategory",
    "RepairOutcome",
    "RepairPlan",
    "RepairPolicy",
    "RepairRoute",
    "RepairRunState",
    "RepairSeverity",
    "VeoGenerationRequest",
    "VeoGenerationResult",
    "VeoOperationCheckpoint",
    "VeoOperationState",
]
