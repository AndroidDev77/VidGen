"""Request and response shapes for T18 shot inspection and regeneration."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from services.animation.reconciliation import MAX_NOTE_LENGTH as MAX_RECONCILIATION_NOTE
from vidgen.contracts.animation import (
    MAX_RECONCILED_ITEMS,
    AmbiguousSubmissionReconciliationReport,
)
from vidgen.contracts.review import (
    ShotDetailProjection,
    ShotRegenerationResult,
    ShotStatusProjection,
    StoryboardShotProjection,
)

__all__ = [
    "ReconcileAmbiguousAnimationRequest",
    "ReconcileAmbiguousAnimationResponse",
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
ReconcileAmbiguousAnimationResponse = AmbiguousSubmissionReconciliationReport


class ShotListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[StoryboardShotProjection]


class RegenerateShotRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm_invalidation: bool = False


class SelectShotAttemptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attempt_id: UUID


class ReconcileAmbiguousAnimationRequest(BaseModel):
    """Release shots stranded on an ambiguous Runway submission.

    Sent without ``verified_no_remote_task`` this is a dry run: it reports every
    stranded shot and what each one still needs. Sending it as ``true`` is the
    caller stating that they confirmed against the provider that no task exists
    for these submissions, which is the only thing that can stand in for a
    lookup the provider does not offer. It is persisted against every attempt it
    releases, so releasing a submission that *was* accepted - and paying for the
    duplicate that the next retry creates - is attributable rather than
    anonymous.
    """

    model_config = ConfigDict(extra="forbid")
    #: Restrict the pass to these shots. Empty means every stranded shot.
    shot_ids: list[UUID] = Field(default_factory=list, max_length=MAX_RECONCILED_ITEMS)
    verified_no_remote_task: bool = False
    #: How the confirmation was made, recorded with each released attempt.
    note: str = Field(default="", max_length=MAX_RECONCILIATION_NOTE)
