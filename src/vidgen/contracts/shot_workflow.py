"""Compact, versioned contracts for T16 Temporal orchestration."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from vidgen.contracts.common import StrictContract


class ShotWorkflowStatus(StrEnum):
    DEFINED = "defined"
    PROMPTING = "prompting"
    KEYFRAME_GENERATING = "keyframe_generating"
    KEYFRAME_QA = "keyframe_qa"
    ANIMATING = "animating"
    VIDEO_QA = "video_qa"
    # T21 repair and fallback routing. A failed T20 video QA result starts or
    # resumes a bounded repair for that one shot; siblings are untouched.
    REPAIR_PLANNING = "repair_planning"
    REPAIRING = "repairing"
    ALTERNATE_PROVIDER = "alternate_provider"
    FALLBACK_RENDERING = "fallback_rendering"
    REVALIDATING = "revalidating"
    HUMAN_REVIEW_REQUIRED = "human_review_required"
    REPAIR_FAILED = "repair_failed"
    LOCKED = "locked"
    FAILED = "failed"
    CANCELLED = "cancelled"


#: The T21 states a repair run can leave a shot in. A shot only reaches
#: ``LOCKED`` when the selected output passed its own T20 evaluation.
REPAIR_TERMINAL_STATES = frozenset(
    {
        ShotWorkflowStatus.LOCKED,
        ShotWorkflowStatus.HUMAN_REVIEW_REQUIRED,
        ShotWorkflowStatus.REPAIR_FAILED,
    }
)


class ShotFailureClass(StrEnum):
    TRANSIENT_PROVIDER_FAILURE = "transient_provider_failure"
    RATE_LIMIT = "rate_limit"
    PROVIDER_TIMEOUT = "provider_timeout"
    POLLING_INTERRUPTION = "polling_interruption"
    DOWNLOAD_INTERRUPTION = "download_interruption"
    WORKER_INTERRUPTION = "worker_interruption"
    TEMPORARY_STORAGE_FAILURE = "temporary_storage_failure"
    BUDGET_DENIAL = "budget_denial"
    DETERMINISTIC_CONFIGURATION_FAILURE = "deterministic_configuration_failure"
    INVALID_LINEAGE = "invalid_lineage"
    UNSUPPORTED_CAPABILITY = "unsupported_capability"
    CORRUPT_PROVIDER_OUTPUT = "corrupt_provider_output"
    TECHNICAL_VALIDATION_FAILURE = "technical_validation_failure"
    # T20 semantic outcomes. A blocked shot is never retried automatically: T21
    # owns repair, and a review-required shot waits for a human decision.
    VISUAL_QA_FAILURE = "visual_qa_failure"
    VISUAL_QA_REVIEW_REQUIRED = "visual_qa_review_required"
    # T21 exhausted its bounded policy without a passing output.
    REPAIR_EXHAUSTED = "repair_exhausted"
    CANCELLATION = "cancellation"
    UNKNOWN_FAILURE = "unknown_failure"


class ShotWorkflowFailure(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    classification: ShotFailureClass
    code: str = Field(min_length=1, max_length=100)
    retryable: bool
    attempt: int = Field(ge=1)
    message: str = Field(max_length=500)


class ShotWorkflowIdentity(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    project_id: UUID
    storyboard_run_id: UUID
    storyboard_input_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    storyboard_shot_id: UUID
    canonical_shot_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    shot_sequence: int = Field(ge=0)
    timing_manifest_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    t14_configuration_identity: str
    t15_capability_profile_identity: str
    t14_pipeline_version: str
    t15_pipeline_version: str
    t16_workflow_version: Literal["t16/1"] = "t16/1"
    attempt_policy_version: Literal["shot-attempt/1"] = "shot-attempt/1"
    identity_hash: str = Field(pattern=r"^[a-f0-9]{64}$")

    def material(self) -> dict[str, str | int]:
        """Return exactly the immutable fields bound by the Temporal identity."""
        return {
            "project_id": str(self.project_id),
            "storyboard_run_id": str(self.storyboard_run_id),
            "storyboard_input_hash": self.storyboard_input_hash,
            "storyboard_shot_id": str(self.storyboard_shot_id),
            "canonical_shot_hash": self.canonical_shot_hash,
            "shot_sequence": self.shot_sequence,
            "timing_manifest_hash": self.timing_manifest_hash,
            "t14_configuration_identity": self.t14_configuration_identity,
            "t15_capability_profile_identity": self.t15_capability_profile_identity,
            "t14_pipeline_version": self.t14_pipeline_version,
            "t15_pipeline_version": self.t15_pipeline_version,
            "t16_workflow_version": self.t16_workflow_version,
            "attempt_policy_version": self.attempt_policy_version,
        }

    @model_validator(mode="after")
    def identity_hash_matches_material(self) -> ShotWorkflowIdentity:
        encoded = json.dumps(self.material(), sort_keys=True, separators=(",", ":")).encode()
        if self.identity_hash != hashlib.sha256(encoded).hexdigest():
            raise ValueError("identity_hash does not bind the material identity fields")
        return self


class ShotWorkflowInput(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    project_id: UUID
    storyboard_run_id: UUID
    storyboard_shot_id: UUID
    shot_input_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    workflow_identity: ShotWorkflowIdentity
    t14_run_id: UUID | None = None
    t15_run_id: UUID | None = None
    parent_workflow_id: str | None = Field(default=None, max_length=255)
    idempotency_key: str = Field(min_length=1, max_length=255)
    trace_context: dict[str, str] = Field(default_factory=dict)
    attempt_policy_version: Literal["shot-attempt/1"] = "shot-attempt/1"

    @model_validator(mode="after")
    def lineage_matches(self) -> ShotWorkflowInput:
        identity = self.workflow_identity
        if (self.project_id, self.storyboard_run_id, self.storyboard_shot_id) != (
            identity.project_id,
            identity.storyboard_run_id,
            identity.storyboard_shot_id,
        ):
            raise ValueError("workflow identity lineage does not match input")
        if self.shot_input_hash != identity.identity_hash:
            raise ValueError("shot_input_hash does not match workflow identity")
        return self


class ShotWorkflowProgress(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    state: ShotWorkflowStatus
    current_stage: str
    current_attempt: int = Field(ge=0)
    retryable: bool = False
    t14_run_id: UUID | None = None
    t15_run_id: UUID | None = None
    selected_keyframe_asset_id: UUID | None = None
    selected_video_asset_id: UUID | None = None
    last_failure: ShotWorkflowFailure | None = None
    last_checkpoint: str | None = None
    started_at: datetime | None = None
    updated_at: datetime | None = None
    cost_microusd: int = Field(default=0, ge=0)
    warning_codes: list[str] = Field(default_factory=list, max_length=32)
    # T21 carries IDs only. Prompts, QA evidence, provider responses, fallback
    # manifests and media never enter Temporal history.
    repair_run_id: UUID | None = None
    selected_repair_attempt_id: UUID | None = None
    human_review_reason: str | None = Field(default=None, max_length=64)

    @field_validator("started_at", "updated_at")
    @classmethod
    def timezone_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("workflow timestamps must be timezone-aware")
        return value


class ShotWorkflowCommand(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    command_id: str = Field(min_length=1, max_length=128)
    project_id: UUID
    storyboard_shot_id: UUID
    command: Literal["inspect", "resume", "retry", "cancel", "regenerate", "outputs"]
    expected_state: ShotWorkflowStatus | None = None
    new_shot_input_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def regeneration_changes_identity(self) -> ShotWorkflowCommand:
        if self.command == "regenerate" and self.new_shot_input_hash is None:
            raise ValueError("regenerate requires new_shot_input_hash")
        return self


class ShotWorkflowCommandResult(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    command_id: str
    accepted: bool
    state: ShotWorkflowStatus
    code: str


class ShotWorkflowQueryResult(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    workflow_id: str
    identity_hash: str
    progress: ShotWorkflowProgress


class ShotWorkflowResult(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    shot_id: UUID
    child_workflow_id: str
    identity_hash: str
    final_state: ShotWorkflowStatus
    t14_run_id: UUID | None = None
    selected_keyframe_asset_id: UUID | None = None
    t15_run_id: UUID | None = None
    selected_video_asset_id: UUID | None = None
    exact_usable_duration_us: int | None = Field(default=None, ge=0)
    provider_generation_duration_us: int | None = Field(default=None, ge=0)
    trim_instructions_asset_id: UUID | None = None
    repair_run_id: UUID | None = None
    selected_repair_attempt_id: UUID | None = None
    human_review_reason: str | None = Field(default=None, max_length=64)
    failure: ShotWorkflowFailure | None = None
    warning_codes: list[str] = Field(default_factory=list, max_length=32)


class ProjectShotFanoutInput(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    project_id: UUID
    storyboard_run_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=220)
    concurrency: int = Field(default=10, ge=1, le=100)
    trace_context: dict[str, str] = Field(default_factory=dict)
    t14_configuration_identity: str = "image-provider/1"
    t15_capability_profile_identity: str = "runway/2024-11-06"
    attempt_policy_version: Literal["shot-attempt/1"] = "shot-attempt/1"


class ProjectShotFanoutResult(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    project_id: UUID
    storyboard_run_id: UUID
    status: Literal[
        "shot_generation_queued",
        "shot_generation_running",
        "shot_generation_partial",
        "shot_generation_retrying",
        "shot_generation_complete",
        "shot_generation_failed",
        "shot_generation_cancelled",
    ]
    results: list[ShotWorkflowResult] = Field(default_factory=list)
    total_count: int = Field(ge=0)
    queued_count: int = Field(ge=0)
    active_count: int = Field(ge=0)
    locked_count: int = Field(ge=0)
    retryable_failure_count: int = Field(ge=0)
    terminal_failure_count: int = Field(ge=0)
    cancelled_count: int = Field(ge=0)
    current_concurrency: int = Field(ge=0)


class ResolveShotFanoutResult(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    shots: list[ShotWorkflowInput]
