"""Request and response shapes for T18 render approval."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from vidgen.contracts.review import RenderApprovalProjection, RenderProjection

__all__ = ["ApproveRenderRequest", "ApproveRenderResponse"]


class ApproveRenderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # The lineage the reviewer actually watched, echoed back from GET /render.
    lineage_hash: str


class ApproveRenderResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approval: RenderApprovalProjection
    render: RenderProjection
