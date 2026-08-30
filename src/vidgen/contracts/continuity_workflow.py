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
    #: The project workflow that owns this reference run, when T19 runs inside
    #: the normal lifecycle. The child signals it on reaching a human pause so
    #: the project's own state stays truthful while it waits.
    parent_workflow_id: str | None = Field(default=None, max_length=255)


class ReferenceDraftResult(StrictContract):
    """What drafting produced, and whether a human decision is actually owed.

    ``requires_approval`` is the deterministic-completion switch. A project with
    no character or location that has reference evidence produces no drafts, and
    the workflow must finish rather than wait forever for an approval nobody can
    give.
    """

    schema_version: Literal["1.0"] = "1.0"
    project_id: UUID
    reference_run_id: UUID
    draft_version_ids: list[UUID] = Field(default_factory=list)
    reused_version_ids: list[UUID] = Field(default_factory=list)
    entity_count: int = Field(default=0, ge=0)
    requires_approval: bool = True
    status: Literal["references_awaiting_approval", "references_complete"] = (
        "references_awaiting_approval"
    )


class ReferenceApprovalSignal(StrictContract):
    """A durable approval decision, delivered to the waiting T19 workflow.

    Carries identifiers only: the approved rows, their lineage and the exact
    storyboard the binding must apply to are all resolved from the database by
    the activity, so no reference payload enters workflow history.
    """

    schema_version: Literal["1.0"] = "1.0"
    project_id: UUID
    reference_run_id: UUID
    approval_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=255)
    storyboard_run_id: UUID | None = None
    #: The exact reference sets the owner approved. A signal naming a set that
    #: is no longer approved is stale and is refused by the activity.
    approved_reference_set_ids: list[UUID] = Field(default_factory=list, max_length=256)


class ReferenceWorkflowResult(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    project_id: UUID
    reference_run_id: UUID
    status: ReferenceWorkflowStatus
    approved_version_ids: list[UUID] = Field(default_factory=list)
    affected_shot_ids: list[UUID] = Field(default_factory=list)
    cancelled: bool = False
