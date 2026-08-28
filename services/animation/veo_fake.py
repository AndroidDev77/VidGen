"""A deterministic, network-free stand-in for the Google Veo adapter.

The fake makes the whole T21 alternate-provider route runnable locally, in the
deterministic test suite and in CI without a Google credential and without a
paid call. It is deterministic by construction: the same request always produces
the same operation name and the same rendered clip, so lineage, cost accounting
and revalidation can be asserted exactly.

It also models the failure modes that make the real adapter hard: a submission
whose outcome is unknown, an operation that needs several polls, an operation
that outlives its polling window, a rate limit, and a safety rejection. Each is
opt-in, so a test names exactly the behaviour it is asserting.
"""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from services.animation.veo import (
    VEO_PROVIDER_NAME,
    VeoCapabilityProfile,
    VeoOperationTimeout,
    VeoRateLimited,
    VeoSubmissionAmbiguous,
    capability_profile,
)
from services.animation.veo_adapter import (
    DownloadedMedia,
    VeoInputImages,
    validate_veo_request,
)
from vidgen.contracts.repair import VeoGenerationRequest, VeoGenerationResult, VeoOperationState


@dataclass(slots=True)
class _Operation:
    request: VeoGenerationRequest
    polls: int = 0


class FakeVeoProvider:
    """A deterministic alternate provider that needs no credentials."""

    def __init__(
        self,
        *,
        model: str | None = None,
        polls_before_completion: int = 1,
        fail: bool = False,
        failure_code: str = "veo_generation_failed",
        safety_reject: bool = False,
        rate_limit_polls: int = 0,
        ambiguous_submission: bool = False,
        never_completes: bool = False,
        corrupt_output: bool = False,
        empty_output: bool = False,
        output_width: int | None = None,
        output_height: int | None = None,
    ) -> None:
        self._profile = capability_profile(model)
        self.polls_before_completion = polls_before_completion
        self.fail = fail
        self.failure_code = failure_code
        self.safety_reject = safety_reject
        self.rate_limit_polls = rate_limit_polls
        self.ambiguous_submission = ambiguous_submission
        self.never_completes = never_completes
        self.corrupt_output = corrupt_output
        self.empty_output = empty_output
        #: Deterministic fixtures use small media; a real Veo model only renders
        #: 720p and 1080p, so an explicit geometry keeps the fake honest about
        #: being a fake rather than pretending the model supports 320x180.
        self.output_width = output_width
        self.output_height = output_height
        #: How many *paid* submissions the fake actually accepted. A resumed
        #: operation must never increment this.
        self.submissions = 0
        self.polls = 0
        self.requests: list[VeoGenerationRequest] = []
        self._operations: dict[str, _Operation] = {}
        self._directory = TemporaryDirectory(prefix="vidgen-fake-veo-")
        self.closed = False

    @property
    def name(self) -> str:
        return VEO_PROVIDER_NAME

    @property
    def model(self) -> str:
        return self._profile.model

    @property
    def capabilities(self) -> VeoCapabilityProfile:
        return self._profile

    async def aclose(self) -> None:
        self.closed = True
        self._directory.cleanup()

    # --- long-running operation ------------------------------------------
    async def submit(self, request: VeoGenerationRequest, images: VeoInputImages) -> str:
        validate_veo_request(request, self._profile)
        del images
        self.requests.append(request)
        if self.ambiguous_submission:
            # The operation may or may not exist. The router must reconcile it
            # rather than paying for a second one.
            raise VeoSubmissionAmbiguous(
                "fake veo submission outcome is unknown after a transport failure"
            )
        name = self._operation_name(request)
        if name not in self._operations:
            self._operations[name] = _Operation(request)
            self._render(name, request)
            self.submissions += 1
        return name

    async def poll(self, operation_name: str) -> VeoGenerationResult:
        operation = self._operations[operation_name]
        operation.polls += 1
        self.polls += 1
        if operation.polls <= self.rate_limit_polls:
            raise VeoRateLimited("fake veo rate limit")
        if self.never_completes:
            raise VeoOperationTimeout(
                "fake veo polling window expired; the operation is still durable"
            )
        if self.fail:
            return VeoGenerationResult(
                application_idempotency_key=operation.request.application_idempotency_key,
                operation_name=operation_name,
                model=self._profile.model,
                state=VeoOperationState.FAILED,
                failure_code=self.failure_code,
                failure_message="deterministic fake failure",
                poll_count=operation.polls,
            )
        if self.safety_reject:
            return VeoGenerationResult(
                application_idempotency_key=operation.request.application_idempotency_key,
                operation_name=operation_name,
                model=self._profile.model,
                state=VeoOperationState.FAILED,
                failure_code="veo_safety_filtered",
                failure_message="the provider filtered every candidate",
                rai_filtered_count=1,
                poll_count=operation.polls,
            )
        if operation.polls < self.polls_before_completion:
            return VeoGenerationResult(
                application_idempotency_key=operation.request.application_idempotency_key,
                operation_name=operation_name,
                model=self._profile.model,
                state=VeoOperationState.RUNNING,
                poll_count=operation.polls,
            )
        return VeoGenerationResult(
            application_idempotency_key=operation.request.application_idempotency_key,
            operation_name=operation_name,
            model=self._profile.model,
            state=VeoOperationState.SUCCEEDED,
            generated_duration_seconds=float(operation.request.duration_seconds),
            has_audio=operation.request.generate_audio,
            output_count=0 if self.empty_output else 1,
            poll_count=operation.polls,
            usage={"video_output_seconds": float(operation.request.duration_seconds)},
            redacted_metadata={"provider": VEO_PROVIDER_NAME, "model": self._profile.model},
        )

    async def download(self, operation_name: str, destination: Path) -> DownloadedMedia:
        source = self._path(operation_name)
        if not source.exists():
            raise ValueError("veo_missing_output: the completed operation carries no video")
        digest = hashlib.sha256()
        total = 0
        with source.open("rb") as reader, destination.open("wb") as writer:
            while chunk := reader.read(1024 * 1024):
                total += len(chunk)
                digest.update(chunk)
                writer.write(chunk)
        return DownloadedMedia(destination, digest.hexdigest(), total)

    # --- deterministic media ---------------------------------------------
    def _operation_name(self, request: VeoGenerationRequest) -> str:
        digest = hashlib.sha256(request.application_idempotency_key.encode()).hexdigest()[:32]
        return (
            f"projects/fake/locations/us-central1/publishers/google/models/"
            f"{self._profile.model}/operations/{digest}"
        )

    def _path(self, operation_name: str) -> Path:
        digest = hashlib.sha256(operation_name.encode()).hexdigest()[:24]
        return Path(self._directory.name) / f"{digest}.mp4"

    def _render(self, operation_name: str, request: VeoGenerationRequest) -> None:
        output = self._path(operation_name)
        if self.empty_output:
            return
        if self.corrupt_output:
            output.write_bytes(b"not-an-mp4" * 32)
            return
        width, height = _dimensions(request.aspect_ratio, request.resolution)
        if self.output_width is not None and self.output_height is not None:
            width, height = self.output_width, self.output_height
        colour = f"0x{hashlib.sha256(request.prompt_hash.encode()).hexdigest()[:6]}"
        completed = subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"color=c={colour}:s={width}x{height}:r=24:d={request.duration_seconds}",
                "-frames:v",
                str(24 * request.duration_seconds),
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                "-threads",
                "1",
                "-map_metadata",
                "-1",
                "-fflags",
                "+bitexact",
                "-movflags",
                "+faststart",
                str(output),
            ],
            capture_output=True,
            check=False,
        )
        if completed.returncode:
            raise RuntimeError(completed.stderr.decode(errors="replace")[:512])


def _dimensions(aspect_ratio: str, resolution: str) -> tuple[int, int]:
    height = 1080 if resolution == "1080p" else 720
    left, right = (int(value) for value in aspect_ratio.split(":"))
    width = round(height * left / right)
    return width - (width % 2), height


__all__ = ["FakeVeoProvider"]
