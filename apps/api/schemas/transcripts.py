"""Request and response shapes for T18 transcript review."""

from __future__ import annotations

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
    model_config = ConfigDict(extra="forbid")
    segment: TranscriptSegmentProjection
    transcript_row_version: int
    invalidation: InvalidationSet
