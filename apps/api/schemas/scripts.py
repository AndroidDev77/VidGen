"""Request and response shapes for T18 script review and versioning."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from vidgen.contracts.review import (
    InvalidationSet,
    ScriptProjection,
    ScriptSegmentProjection,
    ScriptSummaryProjection,
)

__all__ = [
    "ScriptListResponse",
    "ScriptResponse",
    "SelectScriptResponse",
    "UpdateScriptSegmentRequest",
    "UpdateScriptSegmentResponse",
]

ScriptResponse = ScriptProjection


class ScriptListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[ScriptSummaryProjection]


class UpdateScriptSegmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str | None = Field(default=None, max_length=20_000)
    visual_gag: str | None = Field(default=None, max_length=2_000)
    confirm_invalidation: bool = False


class UpdateScriptSegmentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    segment: ScriptSegmentProjection
    script: ScriptSummaryProjection
    created_version: bool
    invalidation: InvalidationSet


class SelectScriptResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    script: ScriptSummaryProjection
