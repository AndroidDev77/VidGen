from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator

from vidgen.contracts.common import StrictContract


class FailureClass(StrEnum):
    TRANSIENT = "transient"
    PERMANENT = "permanent"
    VALIDATION = "validation"
    QUOTA = "quota"
    PROVIDER = "provider"
    CANCELLED = "cancelled"


class WorkflowFailure(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    error_class: FailureClass
    code: str
    message: str
    retryable: bool
    details: dict[str, object] = Field(default_factory=dict)


class ProjectWorkflowInput(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    project_id: UUID
    source_video_id: UUID
    # Leave room for the longest generated ``:<stage>`` suffix.
    idempotency_key: str = Field(min_length=1, max_length=220)
    provider_configuration_version: str = "runway/2024-11-06"
    trace_context: dict[str, str] = Field(default_factory=dict)


class StageActivityInput(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    project_id: UUID
    source_video_id: UUID
    stage: str
    idempotency_key: str = Field(min_length=1, max_length=255)


class AnimationActivityInput(StrictContract):
    """Compact T15 message; large canonical payloads stay in durable storage."""

    schema_version: Literal["1.0"] = "1.0"
    project_id: UUID
    storyboard_id: UUID | None
    image_generation_run_id: UUID | None
    animation_run_id: UUID
    provider_configuration_version: str
    idempotency_key: str = Field(min_length=1, max_length=255)
    trace_context: dict[str, str] = Field(default_factory=dict)


class RenderActivityInput(StrictContract):
    """Compact T17b message. Manifests, captions, media bytes, FFmpeg output and
    diagnostics never enter workflow history: the activity resolves everything
    from durable storage using these IDs alone."""

    schema_version: Literal["1.0"] = "1.0"
    project_id: UUID
    render_job_id: UUID | None = None
    idempotency_key: str = Field(min_length=1, max_length=255)
    trace_context: dict[str, str] = Field(default_factory=dict)


class RenderActivityResult(StrictContract):
    """The bounded T17b outcome the workflow may branch on. IDs and counts only."""

    schema_version: Literal["1.0"] = "1.0"
    project_id: UUID
    render_job_id: UUID
    status: str = Field(min_length=1, max_length=48)
    reused: bool = False
    progress_percent: int = Field(default=0, ge=0, le=100)
    render_identity: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    input_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    output_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    final_render_asset_id: UUID | None = None
    render_manifest_asset_id: UUID | None = None
    measured_duration_us: int | None = Field(default=None, gt=0)
    attempt: int = Field(default=0, ge=0)
    error_code: str | None = Field(default=None, max_length=128)
    failure_classification: str | None = Field(default=None, max_length=32)


class FinalQAActivityInput(StrictContract):
    """Compact T22 message. Scripts, captions, media bytes, reports, sampled
    frames, provider payloads and findings never enter workflow history: the
    activity resolves everything from durable storage using these IDs alone."""

    schema_version: Literal["1.0"] = "1.0"
    project_id: UUID
    final_render_asset_id: UUID | None = None
    render_manifest_asset_id: UUID | None = None
    final_editorial_run_id: UUID | None = None
    provider: Literal["fake", "openai"] = "fake"
    adjudicate: bool = True
    idempotency_key: str = Field(min_length=1, max_length=255)
    trace_context: dict[str, str] = Field(default_factory=dict)


class FinalQAActivityResult(StrictContract):
    """The bounded T22 outcome the workflow may branch on. IDs and counts only."""

    schema_version: Literal["1.0"] = "1.0"
    project_id: UUID
    final_editorial_run_id: UUID
    final_render_asset_id: UUID
    status: str = Field(min_length=1, max_length=48)
    phase: str = Field(min_length=1, max_length=32)
    decision: Literal["PASS", "FAIL", "REVIEW"] | None = None
    blocking_finding_count: int = Field(default=0, ge=0)
    review_finding_count: int = Field(default=0, ge=0)
    deterministic_failure_count: int = Field(default=0, ge=0)
    remediation_targets: list[str] = Field(default_factory=list, max_length=16)
    report_asset_id: UUID | None = None
    cost_microusd: int = Field(default=0, ge=0)
    error_code: str | None = Field(default=None, max_length=128)
    reused: bool = False


class StageActivityResult(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    stage: str
    entity_id: UUID | None = None
    asset_id: UUID | None = None
    reused: bool = False


class ProjectWorkflowState(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    project_id: UUID
    status: str
    completed_stages: list[str] = Field(default_factory=list)
    cancelled: bool = False
    failure: WorkflowFailure | None = None
    updated_at: datetime | None = None

    @field_validator("updated_at")
    @classmethod
    def require_utc(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("updated_at must be timezone-aware")
        return value
