"""The restartable T25 publication workflow.

Started only after T22 passes and the user explicitly asks to publish. The
workflow itself holds nothing but IDs: it decides which activity runs next from
the status each activity returns, and every piece of real state lives in the
database the activities read and write.

The upload step is deliberately re-entrant. ``upload_chunks`` returns as soon as
it has made progress or hit a waiting state, and the workflow calls it again
from the *server-confirmed* offset. A worker that dies mid-upload therefore
costs one activity attempt, not one upload.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from packages.workflows.retry_policies import (
        default_activity_retry_policy,
        provider_activity_retry_policy,
    )
    from vidgen.contracts.publication import (
        PublicationActivityInput,
        PublicationActivityResult,
        PublicationStatus,
    )

#: The dedicated task queue. Long uploads run here so they cannot starve the
#: ordinary project activities on ``vidgen-projects``.
PUBLISHER_TASK_QUEUE = "vidgen-publisher"

#: A single chunk-upload attempt is bounded; the workflow loops instead.
_UPLOAD_TIMEOUT = timedelta(hours=2)
_SHORT_TIMEOUT = timedelta(minutes=10)
_PROCESSING_TIMEOUT = timedelta(hours=1)

#: How many times the workflow re-enters the upload activity before giving up.
#: Each entry resumes from the confirmed offset, so this bounds wall-clock, not
#: progress.
MAX_UPLOAD_ROUNDS = 64
MAX_PROCESSING_ROUNDS = 32

#: States the workflow stops on: a human, the quota clock or a reconnection has
#: to move them.
_WAITING = frozenset(
    {
        PublicationStatus.HUMAN_REVIEW_REQUIRED,
        PublicationStatus.QUOTA_BLOCKED,
        PublicationStatus.REAUTHORIZATION_REQUIRED,
        PublicationStatus.PROCESSING_FAILED,
        PublicationStatus.FAILED,
        PublicationStatus.CANCELLED,
    }
)


@workflow.defn(name="YouTubePublicationWorkflow")
class YouTubePublicationWorkflow:
    """Drives one publication from eligibility to a verified private video."""

    def __init__(self) -> None:
        self._state: PublicationActivityResult | None = None
        self._cancelled = False

    @workflow.query
    def state(self) -> PublicationActivityResult | None:
        """The last ID-only projection an activity returned."""
        return self._state

    @workflow.signal
    def cancel_publication(self) -> None:
        """Stop before the next activity. An uploaded video is never deleted."""
        self._cancelled = True

    async def _step(
        self, name: str, request: PublicationActivityInput, start_to_close: timedelta
    ) -> PublicationActivityResult:
        policy = (
            provider_activity_retry_policy()
            if name != "validate_publication_eligibility_activity"
            else default_activity_retry_policy()
        )
        result: PublicationActivityResult = await workflow.execute_activity(
            name,
            request,
            start_to_close_timeout=start_to_close,
            heartbeat_timeout=timedelta(minutes=2),
            retry_policy=policy,
            result_type=PublicationActivityResult,
        )
        self._state = result
        return result

    @workflow.run
    async def run(self, request: PublicationActivityInput) -> PublicationActivityResult:
        result = await self._step(
            "validate_publication_eligibility_activity", request, _SHORT_TIMEOUT
        )
        if result.status in _WAITING:
            return result
        result = await self._step(
            "refresh_publication_connection_activity", request, _SHORT_TIMEOUT
        )
        if result.status in _WAITING:
            return result
        result = await self._step("initialize_publication_upload_activity", request, _SHORT_TIMEOUT)

        for _ in range(MAX_UPLOAD_ROUNDS):
            if self._cancelled:
                return await self._step("finalize_publication_activity", request, _SHORT_TIMEOUT)
            if result.status in _WAITING or result.video_id:
                break
            result = await self._step(
                "upload_publication_chunks_activity", request, _UPLOAD_TIMEOUT
            )
        if result.status in _WAITING or not result.video_id:
            return result

        for _ in range(MAX_PROCESSING_ROUNDS):
            result = await self._step(
                "poll_publication_processing_activity", request, _PROCESSING_TIMEOUT
            )
            if result.status in _WAITING or result.status != PublicationStatus.PROCESSING:
                break
        if result.status in _WAITING:
            return result

        # Captions and the thumbnail come after the video exists, so neither can
        # ever cause a second upload.
        result = await self._step("upload_publication_captions_activity", request, _SHORT_TIMEOUT)
        if result.status in _WAITING:
            return result
        result = await self._step("upload_publication_thumbnail_activity", request, _SHORT_TIMEOUT)
        if result.status in _WAITING:
            return result
        result = await self._step("verify_publication_private_activity", request, _SHORT_TIMEOUT)
        if result.status in _WAITING:
            return result
        # The workflow stops at a verified private video. Making it unlisted or
        # public is a separate, explicit user action.
        return await self._step("finalize_publication_activity", request, _SHORT_TIMEOUT)
