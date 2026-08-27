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
) -> VideoProviderTask:
    for _ in range(max_polls):
        task = await provider.retrieve(remote_task_id)
        if checkpoint:
            await checkpoint(task)
        if task.status in {
            VideoTaskStatus.SUCCEEDED,
            VideoTaskStatus.FAILED,
            VideoTaskStatus.CANCELLED,
        }:
            return task
        await asyncio.sleep(interval_seconds)
    raise PollingWindowExpired(f"polling window expired; resume remote task {remote_task_id}")
