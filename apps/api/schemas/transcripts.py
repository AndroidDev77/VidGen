"""Request and response shapes for T18 transcript review."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from vidgen.contracts.review import (
    InvalidationSet,
    TranscriptProjection,
    TranscriptSegmentProjection,
)

__all__ = [
    "TranscriptResponse",
    "UpdateTranscriptSegmentRequest",
    "UpdateTranscriptSegmentResponse",
]

TranscriptResponse = TranscriptProjection


class UpdateTranscriptSegmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str | None = Field(default=None, max_length=10_000)
    speaker_label: str | None = Field(default=None, max_length=64)
    confirm_invalidation: bool = False


class UpdateTranscriptSegmentResponse(BaseModel):
    """The edited segment, what it invalidated, and the rebuild that will run.

    The edit itself is complete when this returns: the segment is saved and its
    provenance preserved. ``rebuild_command_id`` is the durable command that
    regenerates the invalidated lineage, present whenever the owner confirmed an
    invalidation that has anything to rebuild.
    """

    model_config = ConfigDict(extra="forbid")
    segment: TranscriptSegmentProjection
    transcript_row_version: int
    invalidation: InvalidationSet
    rebuild_command_id: UUID | None = None
    rebuild_command_status: str | None = Field(default=None, max_length=32)
    #: The earliest stage the rebuild executes. Everything above it is reused.
    rebuild_entry_stage: str | None = Field(default=None, max_length=64)
