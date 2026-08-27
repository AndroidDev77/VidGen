"""Official Runway Python SDK adapter with canonical response mapping."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

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


class RunwayVideoProvider:
    name = "runway"

    def __init__(self, client: Any):
        self._client = client

    async def submit(self, request: VideoProviderRequest, prompt_image: str) -> VideoProviderTask:
        validate_request(request)
        result = await self._client.image_to_video.create(
            model=request.model.value,
            prompt_image=prompt_image,
            prompt_text=request.compiled_motion_prompt,
            duration=request.requested_duration_seconds,
            ratio=f"{request.width}:{request.height}",
        )
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
        result = await self._client.tasks.retrieve(remote_task_id)
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
        )

    async def cancel(self, remote_task_id: str) -> bool:
        await self._client.tasks.delete(remote_task_id)
        return True
