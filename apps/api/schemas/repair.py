"""Bounded T21 repair API projections.

Projections are compact on purpose: no prompts, no provider payloads, no signed
URLs, no video bytes and no fallback manifest blobs. The prompt change is shown
as a structured, already-classified delta - which clauses were added, removed or
rewritten and which constraints were preserved - never as free text a viewer
could mistake for the prompt itself.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import Field

from vidgen.contracts.common import StrictContract


class RepairPromptDeltaProjection(StrictContract):
    """A safe structured view of what one repair changed in the prompt."""

    planner_version: str
    repair_reason: str = ""
    added_clauses: list[str] = Field(default_factory=list)
    removed_clauses: list[str] = Field(default_factory=list)
    rewritten_clauses: list[list[str]] = Field(default_factory=list)
    preserved_constraint_ids: list[str] = Field(default_factory=list)
    touched_constraint_ids: list[str] = Field(default_factory=list)
    before_prompt_hash: str
    after_prompt_hash: str
    seed_changed: bool = False
    previous_seed: int | None = None
    new_seed: int | None = None


class RepairAttemptProjection(StrictContract):
    """One immutable historical attempt in the lineage."""

    attempt_id: UUID
    attempt_ordinal: int = Field(ge=0, le=4)
    attempt_kind: str
    status: str
    predecessor_attempt_id: UUID | None = None
    root_animation_attempt_id: UUID
    provider: str = ""
    model: str = ""
    provider_operation_id: str | None = None
    capability_profile_hash: str | None = None
    prompt_hash: str | None = None
    prompt_delta: RepairPromptDeltaProjection | None = None
    seed: int | None = None
    output_asset_ids: list[UUID] = Field(default_factory=list)
    output_qa_result_id: UUID | None = None
    qa_score: float | None = None
    qa_outcome: str | None = None
    estimated_cost: Decimal = Field(ge=0)
    actual_cost: Decimal = Field(ge=0)
    currency: str
    failure_category: str | None = None
    failure_code: str | None = None
    selected: bool = False
    created_at: datetime
    completed_at: datetime | None = None


class RepairDecisionProjection(StrictContract):
    decision_id: UUID
    sequence: int = Field(ge=0)
    route: str
    rationale: list[str] = Field(default_factory=list)
    failure_category: str | None = None
    repair_codes: list[str] = Field(default_factory=list)
    human_review_reason: str | None = None
    estimated_next_cost: Decimal = Field(ge=0)
    budget_remaining: Decimal | None = None
    planner_version: str
    policy_version: str
    created_at: datetime


class RepairFallbackProjection(StrictContract):
    """What a deterministic 2.5D fallback render actually produced."""

    repair_attempt_id: UUID
    renderer_version: str
    render_identity: str
    input_asset_ids: list[UUID] = Field(default_factory=list)
    exact_duration_us: int = Field(ge=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    frame_rate: str
    pixel_format: str
    video_codec: str
    output_asset_id: UUID
    manifest_asset_id: UUID
    qa_result_id: UUID | None = None


class RepairBudgetProjection(StrictContract):
    currency: str
    total_repair_cost: Decimal = Field(ge=0)
    estimated_repair_cost: Decimal = Field(ge=0)
    per_shot_repair_cost_limit: Decimal | None = None
    project_hard_cap: Decimal | None = None
    project_remaining: Decimal | None = None


class RepairRunProjection(StrictContract):
    """The compact row the dashboard lists next to a failed shot."""

    repair_run_id: UUID
    project_id: UUID
    shot_id: UUID
    state: str
    root_animation_attempt_id: UUID
    triggering_qa_result_id: UUID
    failure_category: str | None = None
    failure_severity: str | None = None
    repair_code: str | None = None
    qa_score: float | None = None
    pass_threshold: float | None = None
    hard_failure: bool = False
    hard_failure_reason: str | None = None
    total_attempt_count: int = Field(ge=0)
    same_provider_repairs_used: int = Field(ge=0)
    alternate_provider_attempts_used: int = Field(ge=0)
    fallback_renders_used: int = Field(ge=0)
    selected_attempt_id: UUID | None = None
    selected_asset_id: UUID | None = None
    final_qa_result_id: UUID | None = None
    final_qa_score: float | None = None
    human_review_reason: str | None = None
    human_review_resolved: bool = False
    policy_version: str
    planner_version: str
    row_version: int = Field(gt=0)
    created_at: datetime
    updated_at: datetime


class RepairRunDetailProjection(RepairRunProjection):
    attempts: list[RepairAttemptProjection] = Field(default_factory=list)
    decisions: list[RepairDecisionProjection] = Field(default_factory=list)
    fallback: RepairFallbackProjection | None = None
    budget: RepairBudgetProjection


class RepairCollectionResponse(StrictContract):
    project_id: UUID
    items: list[RepairRunProjection] = Field(default_factory=list)


class RepairActionRequest(StrictContract):
    """One owner action on a repair run.

    ``retry`` resumes a technical operation that is already durable; it never
    starts a new paid generation on its own. ``cancel`` stops the run before the
    next paid attempt. ``acknowledge`` and ``resolve`` record a human decision on
    a review state. ``restart_after_reference_correction`` re-opens a run whose
    upstream T19 reference has since been corrected.

    No action can mark a hard-failing visual as passed: selection requires a new
    valid T20 result, which only a repair attempt can produce.
    """

    action: Literal[
        "retry",
        "cancel",
        "acknowledge",
        "resolve",
        "restart_after_reference_correction",
    ]
    reason: str = Field(default="", max_length=500)


class RepairActionResponse(StrictContract):
    """A recorded T21 decision, and the command that acts on it.

    ``acknowledge`` and ``resolve`` deliberately create no command and never
    claim the shot passed: they record that a human has seen the review and
    close the prompt, leaving the shot exactly as T20 and T21 left it. Only
    ``retry`` and ``restart_after_reference_correction`` produce executable
    work, because only those ask for the shot to be repaired again.
    """

    repair_run_id: UUID
    action: str
    accepted: bool
    state: str
    code: str
    row_version: int = Field(gt=0)
    continuation_command_id: UUID | None = None
    continuation_command_status: str | None = Field(default=None, max_length=32)
