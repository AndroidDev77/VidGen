"""Request and response shapes for the T18 final render."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from vidgen.contracts.review import RenderProjection

__all__ = ["RenderResponse", "StartRenderRequest", "StartRenderResponse"]

RenderResponse = RenderProjection


class StartRenderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm_invalidation: bool = False


class StartRenderResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    render: RenderProjection
