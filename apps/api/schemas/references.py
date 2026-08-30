"""Bounded T19 continuity API projections."""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import Field

from vidgen.contracts.common import StrictContract


class ReferenceMutationRequest(StrictContract):
    provider: str = "fake"
    model: str = "fake-v1"


class ReferenceDecisionRequest(StrictContract):
    upstream_lineage_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    confirm_invalidation: bool = False


class ReferenceInvalidationProjection(StrictContract):
    affected_shot_ids: list[UUID] = Field(default_factory=list)
    preserved_shot_ids: list[UUID] = Field(default_factory=list)
    stale_keyframe_ids: list[UUID] = Field(default_factory=list)
    stale_video_ids: list[UUID] = Field(default_factory=list)
    stale_render_ids: list[UUID] = Field(default_factory=list)
    estimated_cost_microusd: int = Field(default=0, ge=0)


class ReferenceMutationResponse(StrictContract):
    """The outcome of a T19 mutation, with the command that will execute it.

    ``command_id`` and ``command_status`` are what make ``queued`` true: they
    name a durable row a dispatcher can claim, not an intention. ``workflow_id``
    is absent until the dispatcher has actually started the workflow, and is
    never a calculated value.
    """

    status: Literal["queued", "approved", "rejected", "applied"]
    resource_id: UUID
    row_version: int = Field(gt=0)
    invalidation: ReferenceInvalidationProjection
    command_id: UUID | None = None
    command_status: str | None = Field(default=None, max_length=32)
    workflow_id: str | None = Field(default=None, max_length=255)


class ReferenceCollectionResponse(StrictContract):
    project_id: UUID
    characters: list[dict[str, Any]] = Field(default_factory=list)
    locations: list[dict[str, Any]] = Field(default_factory=list)
    bindings: list[dict[str, Any]] = Field(default_factory=list)
