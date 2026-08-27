"""Deterministic asynchronous provider used without credentials or network calls."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from vidgen.contracts.animation import VideoProviderRequest, VideoProviderTask, VideoTaskStatus


class FakeVideoProvider:
    name = "fake"

    def __init__(self, *, polls_before_completion: int = 1, fail: bool = False):
        self.polls_before_completion, self.fail, self.submissions = polls_before_completion, fail, 0
        self._tasks: dict[str, tuple[VideoProviderRequest, int]] = {}

    async def submit(self, request: VideoProviderRequest, prompt_image: str) -> VideoProviderTask:
        del prompt_image
        remote_id = (
            "fake_" + hashlib.sha256(request.application_idempotency_key.encode()).hexdigest()[:24]
        )
        self._tasks.setdefault(remote_id, (request, 0))
        self.submissions += 1
        return self._task(remote_id, VideoTaskStatus.PENDING)

    async def retrieve(self, remote_task_id: str) -> VideoProviderTask:
        request, polls = self._tasks[remote_task_id]
        polls += 1
        self._tasks[remote_task_id] = (request, polls)
        status = (
            VideoTaskStatus.FAILED
            if self.fail
            else (
                VideoTaskStatus.SUCCEEDED
                if polls >= self.polls_before_completion
                else VideoTaskStatus.RUNNING
            )
        )
        return self._task(remote_task_id, status)

    async def cancel(self, remote_task_id: str) -> bool:
        return remote_task_id in self._tasks

    def _task(self, remote_id: str, status: VideoTaskStatus) -> VideoProviderTask:
        request, _ = self._tasks[remote_id]
        now = datetime.now(UTC)
        return VideoProviderTask(
            provider=request.provider,
            model=request.model,
            remote_task_id=remote_id,
            requested_at=now,
            status=status,
            attempt_number=request.attempt_number,
            requested_duration_seconds=request.requested_duration_seconds,
            completed_at=now
            if status in {VideoTaskStatus.SUCCEEDED, VideoTaskStatus.FAILED}
            else None,
            last_polled_at=now,
            application_idempotency_key=request.application_idempotency_key,
            provider_configuration_version=request.provider_configuration_version,
        )
