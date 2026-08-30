"""Request and response shapes for T18 script review and versioning."""

from __future__ import annotations

from uuid import UUID

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
    """The edited segment and the exact rebuild selecting it would run.

    No command is created here on purpose. A script change is the last point
    before narration spends money, so the rebuild starts only when the owner
    explicitly selects the revised version - ``rebuild_entry_stage`` is what
    they are agreeing to when they do.
    """

    model_config = ConfigDict(extra="forbid")
    segment: ScriptSegmentProjection
    script: ScriptSummaryProjection
    created_version: bool
    invalidation: InvalidationSet
    rebuild_entry_stage: str | None = Field(default=None, max_length=64)


class SelectScriptResponse(BaseModel):
    """The selected script version, and the rebuild that selection started."""

    model_config = ConfigDict(extra="forbid")
    script: ScriptSummaryProjection
    rebuild_command_id: UUID | None = None
    rebuild_command_status: str | None = Field(default=None, max_length=32)
    rebuild_entry_stage: str | None = Field(default=None, max_length=64)
