"""Bounded polling of YouTube's post-upload processing.

Processing is the slowest part of a publication and the one most likely to be
misread. Two rules keep it honest:

* **The video ID is persisted before the first poll.** Everything here assumes
  it: a poll that fails, a worker that dies, a timeout - none of them can lose
  the identity of what was created, so none of them can lead to a second upload.
* **Slow is not failed.** Exceeding the elapsed budget leaves the publication in
  ``PROCESSING`` with its last observed state recorded. Only YouTube saying
  ``failed`` or ``terminated``, or reporting a rejected upload, is a failure -
  and even then the video ID and its watch URL are kept for investigation.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from services.publisher import youtube as capabilities
from services.publisher.contracts import ProcessingSnapshot, YouTubeProvider, YouTubeProviderError
from services.publisher.credentials import SecretValue
from vidgen.contracts.publication import ProcessingState

#: How YouTube's ``processingStatus`` and ``uploadStatus`` map onto the
#: normalized states the rest of the system reasons about.
_PROCESSING_STATES: dict[str, ProcessingState] = {
    capabilities.ProcessingStatus.PROCESSING.value: ProcessingState.PROCESSING,
    capabilities.ProcessingStatus.SUCCEEDED.value: ProcessingState.SUCCEEDED,
    capabilities.ProcessingStatus.FAILED.value: ProcessingState.FAILED,
    capabilities.ProcessingStatus.TERMINATED.value: ProcessingState.FAILED,
}


def normalize(snapshot: ProcessingSnapshot) -> ProcessingState:
    """The normalized state for one snapshot.

    ``uploadStatus`` outranks ``processingStatus`` when it says ``rejected``:
    a video YouTube refused on policy grounds is not "still processing", however
    the processing field reads.
    """
    if snapshot.upload_status == capabilities.UploadStatus.REJECTED.value:
        return ProcessingState.REJECTED
    if snapshot.upload_status == capabilities.UploadStatus.FAILED.value:
        return ProcessingState.FAILED
    return _PROCESSING_STATES.get(snapshot.processing_status, ProcessingState.UNKNOWN)


@dataclass(frozen=True, slots=True)
class ProcessingOutcome:
    """The result of one bounded polling run."""

    state: ProcessingState
    snapshot: ProcessingSnapshot | None
    polls: int
    elapsed_seconds: float
    quota_units: int
    #: True when the elapsed budget ran out while the video was still
    #: processing. Not a failure: the caller waits and polls again later.
    timed_out: bool = False

    @property
    def terminal(self) -> bool:
        return self.state in {
            ProcessingState.SUCCEEDED,
            ProcessingState.FAILED,
            ProcessingState.REJECTED,
        }


class ProcessingPoller:
    """Polls one video's processing status with bounded exponential backoff."""

    def __init__(
        self,
        provider: YouTubeProvider,
        *,
        initial_seconds: float = capabilities.PROCESSING_POLL_INITIAL_SECONDS,
        max_seconds: float = capabilities.PROCESSING_POLL_MAX_SECONDS,
        backoff_factor: float = capabilities.PROCESSING_POLL_BACKOFF_FACTOR,
        max_elapsed_seconds: float = capabilities.PROCESSING_MAX_ELAPSED_SECONDS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        on_snapshot: Callable[[ProcessingSnapshot, ProcessingState], None] | None = None,
    ) -> None:
        self.provider = provider
        self.initial_seconds = initial_seconds
        self.max_seconds = max_seconds
        self.backoff_factor = backoff_factor
        self.max_elapsed_seconds = max_elapsed_seconds
        self.sleep = sleep
        #: Called after every observation so the caller can persist the last
        #: observed state; a worker restart then resumes from what was seen.
        self.on_snapshot = on_snapshot or (lambda snapshot, state: None)

    async def poll(
        self, *, access_token: SecretValue, video_id: str, max_polls: int | None = None
    ) -> ProcessingOutcome:
        delay = self.initial_seconds
        elapsed = 0.0
        polls = 0
        quota = 0
        latest: ProcessingSnapshot | None = None
        state = ProcessingState.UNKNOWN
        while True:
            try:
                latest = await self.provider.fetch_processing_status(
                    access_token=access_token, video_id=video_id
                )
            except YouTubeProviderError as error:
                if not error.retryable:
                    raise
                # A rate limit or a server error during polling never changes
                # what happened to the video; back off and ask again.
                latest = None
            polls += 1
            if latest is not None:
                quota += latest.call.quota_units
                state = normalize(latest)
                self.on_snapshot(latest, state)
                if state in {
                    ProcessingState.SUCCEEDED,
                    ProcessingState.FAILED,
                    ProcessingState.REJECTED,
                }:
                    return ProcessingOutcome(
                        state=state,
                        snapshot=latest,
                        polls=polls,
                        elapsed_seconds=elapsed,
                        quota_units=quota,
                    )
            if max_polls is not None and polls >= max_polls:
                return ProcessingOutcome(
                    state=state,
                    snapshot=latest,
                    polls=polls,
                    elapsed_seconds=elapsed,
                    quota_units=quota,
                    timed_out=True,
                )
            if elapsed + delay > self.max_elapsed_seconds:
                return ProcessingOutcome(
                    state=state,
                    snapshot=latest,
                    polls=polls,
                    elapsed_seconds=elapsed,
                    quota_units=quota,
                    timed_out=True,
                )
            await self.sleep(delay)
            elapsed += delay
            delay = min(delay * self.backoff_factor, self.max_seconds)
