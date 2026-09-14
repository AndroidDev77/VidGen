"""Official Runway Python SDK adapter with canonical response mapping.

Two properties of this adapter are load-bearing and easy to lose:

**The client never outlives the event loop its connections belong to.** Every
Temporal activity runs its coroutine in its own ``asyncio.run`` loop and closes
that loop on return, while the worker builds its providers once at startup. An
``AsyncRunwayML`` shared across activities therefore hands out pooled
connections that belong to a loop which no longer exists, and the first write on
one fails locally with ``RuntimeError: Event loop is closed``. Constructing this
provider with a *client factory* makes it open one client per running loop, so a
single shared provider instance stays correct across any number of loops.

**A local failure is not an ambiguous provider outcome.** The SDK reports every
transport problem as ``APIConnectionError``, which says nothing about whether
the request was written. This adapter reads the cause chain and separates the
failures that provably happened before a single request byte left the process
from the ones that could have reached Runway. Only the latter are ambiguous;
treating the former that way strands a shot forever over a request the provider
never saw.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
from runwayml import APIConnectionError, APITimeoutError

from services.animation.pipeline_errors import AmbiguousVideoSubmission, VideoSubmissionNotSent
from services.animation.providers import validate_request
from vidgen.contracts.animation import (
    RunwayModel,
    VideoProvider,
    VideoProviderRequest,
    VideoProviderTask,
    VideoTaskStatus,
)

_STATUS = {
    "PENDING": VideoTaskStatus.PENDING,
    "THROTTLED": VideoTaskStatus.PENDING,
    "RUNNING": VideoTaskStatus.RUNNING,
    "SUCCEEDED": VideoTaskStatus.SUCCEEDED,
    "FAILED": VideoTaskStatus.FAILED,
    "CANCELLED": VideoTaskStatus.CANCELLED,
}

#: Transport failures that can only occur *before* a request is written: no
#: connection was established (``ConnectError``, ``ConnectTimeout``,
#: ``ProxyError``), none was ever handed out by the pool (``PoolTimeout``), or
#: the request was rejected locally as unroutable (``UnsupportedProtocol``,
#: ``InvalidURL``). Everything else httpx can raise - a read or write timeout, a
#: protocol error, a closed connection mid-exchange - may have left a request
#: with the provider and stays ambiguous.
_PRE_SEND_TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.ProxyError,
    httpx.UnsupportedProtocol,
    httpx.InvalidURL,
)

#: The local ``RuntimeError`` an async client raises when it is asked to reuse a
#: connection opened on an event loop that has since closed. The failure happens
#: as the transport is written to, so nothing reaches the network.
_CLOSED_LOOP_MESSAGE = "event loop is closed"

#: Bound on the cause chain walk, so a pathological chain cannot spin.
_MAX_CAUSE_DEPTH = 16


def _causes(error: BaseException) -> list[BaseException]:
    """``error`` and everything it was raised from, deduplicated and bounded."""
    seen: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and len(seen) < _MAX_CAUSE_DEPTH:
        if any(item is current for item in seen):
            break
        seen.append(current)
        current = current.__cause__ or current.__context__
    return seen


def submission_never_sent(error: BaseException) -> bool:
    """Whether ``error`` proves the submission never left this process.

    Conservative by construction: an unrecognised cause is *not* evidence of
    anything, and the caller must keep treating it as ambiguous. Getting this
    wrong in the permissive direction resubmits a request Runway already
    accepted, which creates and bills a duplicate task.
    """
    for cause in _causes(error):
        if isinstance(cause, _PRE_SEND_TRANSPORT_ERRORS):
            return True
        if isinstance(cause, RuntimeError) and _CLOSED_LOOP_MESSAGE in str(cause).lower():
            return True
    return False


class RunwayVideoProvider:
    name = "runway"

    def __init__(
        self,
        client: Any | None = None,
        *,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        """Adapt either one caller-owned client or a per-loop client factory.

        A caller that passes ``client`` owns its lifetime and must not share the
        instance across event loops. A caller that passes ``client_factory`` -
        which is what the worker does - hands that problem here: one client is
        opened per running loop and released with it.
        """
        if (client is None) == (client_factory is None):
            raise ValueError(
                "RunwayVideoProvider takes exactly one of a client or a client factory"
            )
        self._client = client
        self._factory = client_factory
        # The loop is held by strong reference deliberately: a closed loop that
        # were freed could have its identity reused by the next one, and the
        # comparison below would then match a client bound to a dead loop.
        self._loop: asyncio.AbstractEventLoop | None = None
        self._bound: Any | None = None

    def _current_client(self) -> Any:
        if self._factory is None:
            return self._client
        loop = asyncio.get_running_loop()
        if self._bound is None or self._loop is not loop:
            self._bound = self._factory()
            self._loop = loop
        return self._bound

    async def release_loop_client(self) -> None:
        """Close the client this loop opened, while the loop is still running.

        Called from the activity that owns the loop, so the connection pool is
        shut down on the loop that created it rather than being abandoned to the
        garbage collector. Best effort: a client that refuses to close must not
        turn a completed activity into a failed one.
        """
        if self._factory is None or self._bound is None:
            return
        if self._loop is not asyncio.get_running_loop():
            return
        client, self._bound, self._loop = self._bound, None, None
        closer = getattr(client, "close", None)
        if closer is None:
            return
        try:
            result = closer()
            if inspect.isawaitable(result):
                await result
        except Exception:  # pragma: no cover - defensive: a close never fails work
            return

    async def submit(self, request: VideoProviderRequest, prompt_image: str) -> VideoProviderTask:
        validate_request(request)
        try:
            result = await self._current_client().image_to_video.create(
                model=request.model.value,
                prompt_image=prompt_image,
                prompt_text=request.compiled_motion_prompt,
                duration=int(request.requested_duration_seconds),
                ratio=f"{request.width}:{request.height}",
            )
        except (APIConnectionError, APITimeoutError) as error:
            if submission_never_sent(error):
                raise VideoSubmissionNotSent(
                    "Runway submission failed locally before any request byte was sent"
                ) from error
            raise AmbiguousVideoSubmission(
                "Runway submission transport failed before a remote task ID was received"
            ) from error
        now = datetime.now(UTC)
        return VideoProviderTask(
            provider=request.provider,
            model=request.model,
            remote_task_id=result.id,
            requested_at=now,
            status=VideoTaskStatus.PENDING,
            attempt_number=request.attempt_number,
            requested_duration_seconds=request.requested_duration_seconds,
            application_idempotency_key=request.application_idempotency_key,
            provider_configuration_version=request.provider_configuration_version,
        )

    async def retrieve(self, remote_task_id: str) -> VideoProviderTask:
        result = await self._current_client().tasks.retrieve(remote_task_id)
        now = datetime.now(UTC)
        return VideoProviderTask(
            provider=VideoProvider.RUNWAY,
            model=RunwayModel(getattr(result, "model", "gen4_turbo")),
            remote_task_id=remote_task_id,
            requested_at=getattr(result, "created_at", now),
            status=_STATUS[result.status.upper()],
            attempt_number=1,
            requested_duration_seconds=float(getattr(result, "duration", 2)),
            progress=getattr(result, "progress", None),
            failure_reason=getattr(result, "failure", None),
            provider_error_code=getattr(result, "failure_code", None),
            completed_at=now
            if result.status.upper() in {"SUCCEEDED", "FAILED", "CANCELLED"}
            else None,
            last_polled_at=now,
            application_idempotency_key=getattr(result, "idempotency_key", remote_task_id),
            provider_configuration_version=getattr(result, "configuration_version", "runway-v1"),
            output_handles=tuple(getattr(result, "output", None) or ()),
        )

    async def cancel(self, remote_task_id: str) -> bool:
        await self._current_client().tasks.delete(remote_task_id)
        return True
