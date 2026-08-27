"""Request and response shapes for T18 workflow control."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from vidgen.contracts.review import WorkflowStatusProjection

__all__ = ["StartWorkflowRequest", "StartWorkflowResponse", "WorkflowStatusResponse"]


class StartWorkflowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Ownership is always taken from the authenticated principal, never the body.
    provider_configuration_version: str = Field(
        default="runway/2024-11-06", min_length=1, max_length=64
    )


class StartWorkflowResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workflow_id: str
    run_id: str
    status: WorkflowStatusProjection


WorkflowStatusResponse = WorkflowStatusProjection
