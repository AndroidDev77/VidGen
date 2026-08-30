"""Request and response shapes for T18 workflow control."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from vidgen.contracts.review import WorkflowStatusProjection

__all__ = [
    "ContinueWorkflowRequest",
    "StartWorkflowRequest",
    "StartWorkflowResponse",
    "WorkflowStatusResponse",
]


class StartWorkflowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Ownership is always taken from the authenticated principal, never the body.
    provider_configuration_version: str = Field(
        default="runway/2024-11-06", min_length=1, max_length=64
    )
    subtitle_asset_ids: list[UUID] = Field(default_factory=list, max_length=4)


class StartWorkflowResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workflow_id: str
    run_id: str
    status: WorkflowStatusProjection


WorkflowStatusResponse = WorkflowStatusProjection


class ContinueWorkflowRequest(BaseModel):
    """Resume a paused or partially complete project from a named stage."""

    model_config = ConfigDict(extra="forbid")
    entry_stage: str = Field(min_length=1, max_length=64)
    reason: Literal[
        "review_resolved",
        "partial_fanout",
        "revision",
        "remediation",
        "operator_request",
    ] = "operator_request"
