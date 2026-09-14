"""Official Runway Python SDK adapter with canonical response mapping.

Two properties of this adapter are load-bearing and easy to lose:

**The client never outlives the event loop its connections belong to.** Every
Temporal activity runs its coroutine in its own ``asyncio.run`` loop and closes
that loop on return, while the worker builds its providers once at startup and
runs several activities at a time over them. An ``AsyncRunwayML`` shared across
activities therefore hands out pooled connections that belong to a loop which no
longer exists, and the first write on one fails locally with ``RuntimeError:
Event loop is closed``. Constructing this provider with a *client factory* makes
it open one client per running loop, so a single shared provider instance stays
correct across any number of loops and any number of threads running them.

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
import threading
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

#: The bare ``RuntimeError`` that surfaces when an async client is asked to
#: reuse a connection belonging to an event loop that has since closed.
#:
#: Reaching this adapter *bare* is what makes it evidence. anyio translates a
#: ``RuntimeError`` raised by the transport write itself into
#: ``BrokenResourceError``, which httpx reports as ``WriteError`` - and this
#: adapter keeps every write failure ambiguous, precisely because a partially or
#: fully written request may have reached Runway. So a ``RuntimeError`` that
#: arrives untranslated came from outside the write path: pool maintenance
#: closing a connection of the dead loop before the request was ever sent. The
#: incident this rule was written for agrees - the affected tasks were marked
#: 30-50 ms after creation with no ``POST /v1/image_to_video`` in the worker log.
_CLOSED_LOOP_MESSAGE = "event loop is closed"

#: Bound on the cause chain walk, so a pathological chain cannot spin.
_MAX_CAUSE_DEPTH = 16


def _causes(error: BaseException) -> list[BaseException]:
    """``error`` and everything it was explicitly raised *from*, bounded.

    Only ``__cause__`` is followed. The SDK always re-raises with ``from err``,
    so the chain that matters is explicit, while ``__context__`` merely records
    whatever happened to be in flight - an unrelated ambiguous failure being
    handled when this one was raised would otherwise be read as evidence.
    """
    seen: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and len(seen) < _MAX_CAUSE_DEPTH:
        if any(item is current for item in seen):
            break
        seen.append(current)
        current = current.__cause__
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
        # One client per loop, keyed by the loop itself. A map rather than a
        # single slot because the worker runs several activities at once, each
        # on its own loop in its own thread, over this one provider: a single
        # slot would let those threads evict each other's clients and hand one
        # across loops, which is the failure this provider exists to prevent.
        # The keys are strong references deliberately - a freed loop's identity
        # can be reused by the next one, and the lookup would then match a
        # client bound to a dead loop.
        self._clients: dict[asyncio.AbstractEventLoop, Any] = {}
        self._lock = threading.Lock()

    def _current_client(self) -> Any:
        if self._factory is None:
            return self._client
        loop = asyncio.get_running_loop()
        with self._lock:
            self._forget_closed_loops()
            client = self._clients.get(loop)
            if client is None:
                client = self._clients[loop] = self._factory()
            return client

    def _forget_closed_loops(self) -> None:
        """Drop clients whose loop is gone, so the map cannot grow without end.

        A caller that releases its client on the way out never reaches this. It
        is the backstop for one that does not: the client cannot be closed from
        here - its loop is dead - so the reference is simply dropped.
        """
        for stale in [loop for loop in self._clients if loop.is_closed()]:
            del self._clients[stale]

    async def release_loop_client(self) -> None:
        """Close the client this loop opened, while the loop is still running.

        Called from the activity that owns the loop, so the connection pool is
        shut down on the loop that created it rather than being abandoned to the
        garbage collector. Best effort: a client that refuses to close must not
        turn a completed activity into a failed one.
        """
        if self._factory is None:
            return
        with self._lock:
            client = self._clients.pop(asyncio.get_running_loop(), None)
        if client is None:
            return
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
