"""Response shapes for the T18 project event stream."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from vidgen.contracts.review import ProjectEventProjection

__all__ = ["ProjectEventListResponse", "ProjectEventProjection"]


class ProjectEventListResponse(BaseModel):
    """Polling fallback for clients that cannot hold a Server-Sent Events stream."""

    model_config = ConfigDict(extra="forbid")
    items: list[ProjectEventProjection]
    last_event_id: int
