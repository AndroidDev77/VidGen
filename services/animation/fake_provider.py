"""Deterministic asynchronous provider used without credentials or network calls."""

from __future__ import annotations

import hashlib
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from vidgen.contracts.animation import VideoProviderRequest, VideoProviderTask, VideoTaskStatus


class FakeVideoProvider:
    name = "fake"

    def __init__(
        self,
        *,
        polls_before_completion: int = 1,
        fail: bool = False,
        corrupt: bool = False,
        wrong_dimensions: bool = False,
        wrong_duration: bool = False,
        expire_output: bool = False,
    ) -> None:
        self.polls_before_completion = polls_before_completion
        self.fail = fail
        self.corrupt = corrupt
        self.wrong_dimensions = wrong_dimensions
        self.wrong_duration = wrong_duration
        self.expire_output = expire_output
        self.submissions = 0
        self._tasks: dict[str, tuple[VideoProviderRequest, int]] = {}
        self._directory = TemporaryDirectory(prefix="vidgen-fake-video-")

    async def submit(self, request: VideoProviderRequest, prompt_image: str) -> VideoProviderTask:
        del prompt_image
        remote_id = (
            "fake_" + hashlib.sha256(request.application_idempotency_key.encode()).hexdigest()[:24]
        )
        if remote_id not in self._tasks:
            self._tasks[remote_id] = (request, 0)
            self._generate(remote_id, request)
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
        output = self._path(remote_id)
        if status == VideoTaskStatus.SUCCEEDED and self.expire_output:
            output.unlink(missing_ok=True)
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
            output_handles=(output.as_uri(),) if status == VideoTaskStatus.SUCCEEDED else (),
        )

    def _path(self, remote_id: str) -> Path:
        return Path(self._directory.name) / f"{remote_id}.mp4"

    def _generate(self, remote_id: str, request: VideoProviderRequest) -> None:
        output = self._path(remote_id)
        if self.corrupt:
            output.write_bytes(b"not-an-mp4")
            return
        digest = hashlib.sha256(request.application_idempotency_key.encode()).hexdigest()
        color = f"0x{digest[:6]}"
        width = request.width + 16 if self.wrong_dimensions else request.width
        height = request.height
        duration = (
            request.requested_duration_seconds + 1
            if self.wrong_duration
            else request.requested_duration_seconds
        )
        completed = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"color=c={color}:s={width}x{height}:r=24:d={duration}",
                "-an",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-threads",
                "1",
                "-map_metadata",
                "-1",
                "-movflags",
                "+faststart",
                str(output),
            ],
            capture_output=True,
            check=False,
        )
        if completed.returncode:
            raise RuntimeError(completed.stderr.decode(errors="replace")[:512])
