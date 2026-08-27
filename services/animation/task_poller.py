"""Bounded resumable polling; timeouts never imply provider failure."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from services.animation.providers import VideoGenerationProvider
from vidgen.contracts.animation import VideoProviderTask, VideoTaskStatus


class PollingWindowExpired(TimeoutError):
    pass


async def poll_task(
    provider: VideoGenerationProvider,
    remote_task_id: str,
    *,
    max_polls: int = 20,
    interval_seconds: float = 1,
    checkpoint: Callable[[VideoProviderTask], Awaitable[None]] | None = None,
    cancellation_check: Callable[[], bool] | None = None,
    heartbeat: Callable[[str], None] | None = None,
    max_transient_failures: int = 3,
) -> VideoProviderTask:
    transient_failures = 0
    for _ in range(max_polls):
        if cancellation_check and cancellation_check():
            raise asyncio.CancelledError(f"polling cancelled for remote task {remote_task_id}")
        try:
            task = await provider.retrieve(remote_task_id)
            transient_failures = 0
        except BaseException as error:
            if not _is_transient(error) or transient_failures >= max_transient_failures:
                raise
            transient_failures += 1
            await asyncio.sleep(interval_seconds * (2 ** (transient_failures - 1)))
            continue
        if checkpoint:
            await checkpoint(task)
        if heartbeat:
            heartbeat(remote_task_id)
        if task.status in {
            VideoTaskStatus.SUCCEEDED,
            VideoTaskStatus.FAILED,
            VideoTaskStatus.CANCELLED,
        }:
            return task
        await asyncio.sleep(interval_seconds)
    raise PollingWindowExpired(f"polling window expired; resume remote task {remote_task_id}")


def _is_transient(error: BaseException) -> bool:
    status_code = getattr(error, "status_code", None)
    name = type(error).__name__.lower()
    return (
        isinstance(error, (TimeoutError, ConnectionError))
        or "connection" in name
        or "timeout" in name
        or status_code == 429
        or (isinstance(status_code, int) and status_code >= 500)
    )
