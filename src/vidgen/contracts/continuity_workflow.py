"""Compact ID-only commands safe for Temporal history."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import Field

from vidgen.contracts.common import StrictContract


class BuildReferencesCommand(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    project_id: UUID
    episode_analysis_id: UUID
    storyboard_run_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=255)
    character_id: UUID | None = None
    location_id: UUID | None = None
    trace_context: dict[str, str] = Field(default_factory=dict)


class ApplyReferencesCommand(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    project_id: UUID
    reference_version_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=255)
    trace_context: dict[str, str] = Field(default_factory=dict)


class ReferenceWorkflowStatus(StrEnum):
    QUEUED = "references_queued"
    SELECTING = "references_selecting"
    BUILDING = "references_building"
    GENERATING = "references_generating"
    VALIDATING = "references_validating"
    AWAITING_APPROVAL = "references_awaiting_approval"
    BINDING = "references_binding"
    COMPLETE = "references_complete"
    FAILED = "references_failed"
    CANCELLED = "references_cancelled"


class ReferenceWorkflowInput(StrictContract):
    """The complete, deliberately ID-only Temporal history payload."""

    schema_version: Literal["1.0"] = "1.0"
    project_id: UUID
    episode_analysis_id: UUID
    storyboard_run_id: UUID
    reference_run_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=255)
    trace_context: dict[str, str] = Field(default_factory=dict)


class ReferenceDraftResult(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    project_id: UUID
    reference_run_id: UUID
    draft_version_ids: list[UUID] = Field(default_factory=list)
    status: Literal["references_awaiting_approval"] = "references_awaiting_approval"


class ReferenceApprovalSignal(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    project_id: UUID
    reference_run_id: UUID
    approval_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=255)


class ReferenceWorkflowResult(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    project_id: UUID
    reference_run_id: UUID
    status: ReferenceWorkflowStatus
    approved_version_ids: list[UUID] = Field(default_factory=list)
    affected_shot_ids: list[UUID] = Field(default_factory=list)
    cancelled: bool = False
