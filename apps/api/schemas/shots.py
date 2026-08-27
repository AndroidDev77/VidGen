"""Request and response shapes for T18 shot inspection and regeneration."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict

from vidgen.contracts.review import (
    ShotDetailProjection,
    ShotRegenerationResult,
    ShotStatusProjection,
    StoryboardShotProjection,
)

__all__ = [
    "RegenerateShotRequest",
    "SelectShotAttemptRequest",
    "ShotDetailResponse",
    "ShotListResponse",
    "ShotRegenerationResponse",
    "ShotStatusResponse",
]

ShotDetailResponse = ShotDetailProjection
ShotStatusResponse = ShotStatusProjection
ShotRegenerationResponse = ShotRegenerationResult


class ShotListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[StoryboardShotProjection]


class RegenerateShotRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm_invalidation: bool = False


class SelectShotAttemptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attempt_id: UUID
