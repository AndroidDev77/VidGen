"""The provider-neutral alternate-animation boundary and the Google Veo adapter.

The repair pipeline depends on :class:`AlternateVideoProvider` only. It never
touches an HTTP response, a Google SDK type, an access token or a signed URL: an
adapter turns a :class:`~vidgen.contracts.repair.VeoGenerationRequest` into a
:class:`~vidgen.contracts.repair.VeoGenerationResult` plus a file on disk, and
nothing else crosses the boundary.

Three properties matter more than anything else here:

* **The operation name is durable before the first poll.** ``submit`` returns as
  soon as Vertex AI has confirmed an operation; the caller persists that name in
  the same transaction as the pre-call checkpoint. A worker that dies mid-poll
  resumes the operation it already paid for.
* **An ambiguous submission is never resubmitted.** A network failure that
  leaves us unable to say whether an operation started raises
  :class:`~services.animation.veo.VeoSubmissionAmbiguous`, which the router
  treats as a human-review reason, not a retry.
* **Nothing sensitive is logged or persisted.** Credentials, prompts, signed
  URLs and raw provider payloads never leave this module; only bounded, redacted
  metadata does.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from types import TracebackType
from typing import Any, Protocol

import httpx

from services.animation.veo import (
    DEFAULT_VEO_LOCATION,
    VEO_API_VERSION,
    VEO_POLL_METHOD,
    VEO_PROVIDER_NAME,
    VEO_SUBMIT_METHOD,
    UnsupportedVeoCapability,
    VeoCapabilityProfile,
    VeoOperationTimeout,
    VeoRateLimited,
    VeoSubmissionAmbiguous,
    capability_profile,
    request_limits,
)
from vidgen.contracts.repair import (
    VeoGenerationRequest,
    VeoGenerationResult,
    VeoOperationState,
)

#: Downloads are streamed in bounded chunks; a whole clip is never held in memory.
DOWNLOAD_CHUNK_BYTES = 1024 * 1024
MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class VeoInputImage:
    """One input image handed to the adapter. Bytes never enter a contract."""

    asset_id_hex: str
    content: bytes
    media_type: str = "image/png"


@dataclass(frozen=True, slots=True)
class VeoInputImages:
    """Every image one Veo request may carry, already capability-filtered."""

    first_frame: VeoInputImage | None = None
    last_frame: VeoInputImage | None = None
    references: tuple[VeoInputImage, ...] = ()


@dataclass(frozen=True, slots=True)
class DownloadedMedia:
    path: Path
    sha256: str
    byte_size: int


class AlternateVideoProvider(Protocol):
    """One alternate animation provider, driven as a long-running operation."""

    @property
    def name(self) -> str: ...

    @property
    def model(self) -> str: ...

    @property
    def capabilities(self) -> VeoCapabilityProfile: ...

    async def submit(self, request: VeoGenerationRequest, images: VeoInputImages) -> str:
        """Start a generation and return the durable provider operation name."""

    async def poll(self, operation_name: str) -> VeoGenerationResult:
        """Read the current state of an existing operation. Never submits."""

    async def download(self, operation_name: str, destination: Path) -> DownloadedMedia:
        """Stream the completed output to ``destination``."""

    async def aclose(self) -> None: ...


def validate_veo_request(request: VeoGenerationRequest, profile: VeoCapabilityProfile) -> None:
    """Reject anything the declared capability profile does not support.

    Every check here happens before a paid call. A model that has no last-frame
    control must never be handed one and silently ignore it, because the shot
    would then fail T20 again for a reason the router cannot see.
    """
    if request.model != profile.model:
        raise UnsupportedVeoCapability(
            f"capability_profile_mismatch: request targets {request.model!r}, "
            f"profile declares {profile.model!r}"
        )
    if request.capability_profile_hash != profile.profile_hash:
        raise UnsupportedVeoCapability(
            "capability_profile_mismatch: the request was planned against a different "
            "capability profile version"
        )
    limits = request_limits(profile)
    if request.duration_seconds not in profile.durations_seconds:
        supported = ", ".join(str(value) for value in profile.durations_seconds)
        raise UnsupportedVeoCapability(
            f"unsupported_duration: {request.duration_seconds}s; {profile.model} "
            f"supports {supported}"
        )
    if request.aspect_ratio not in profile.aspect_ratios:
        raise UnsupportedVeoCapability(f"unsupported_aspect_ratio: {request.aspect_ratio}")
    if request.resolution not in profile.resolutions:
        raise UnsupportedVeoCapability(f"unsupported_resolution: {request.resolution}")
    if len(request.prompt) > limits.max_prompt_characters:
        raise UnsupportedVeoCapability("invalid_prompt: provider prompt limit exceeded")
    if request.first_frame_asset_id is not None and not profile.supports_image_to_video:
        raise UnsupportedVeoCapability(f"unsupported_image_to_video: {profile.model}")
    if request.last_frame_asset_id is not None and not profile.supports_last_frame:
        raise UnsupportedVeoCapability(f"unsupported_strict_last_frame: {profile.model}")
    if request.reference_asset_ids and not profile.supports_reference_images:
        raise UnsupportedVeoCapability(f"unsupported_reference_images: {profile.model}")
    if len(request.reference_asset_ids) > limits.max_reference_images:
        raise UnsupportedVeoCapability(
            f"too_many_reference_images: {profile.model} accepts at most "
            f"{limits.max_reference_images}"
        )
    if request.generate_audio and not profile.generates_native_audio:
        raise UnsupportedVeoCapability(f"unsupported_native_audio: {profile.model}")
    if not request.generate_audio and not profile.audio_is_optional:
        raise UnsupportedVeoCapability(
            f"audio_not_optional: {profile.model} always returns a native audio track"
        )


def veo_request_payload(request: VeoGenerationRequest, images: VeoInputImages) -> dict[str, Any]:
    """Render the exact JSON body Vertex AI's ``predictLongRunning`` expects.

    Kept pure and separate from transport so serialization is asserted in tests
    without a network call, a credential, or a mocked HTTP stack.
    """
    instance: dict[str, Any] = {"prompt": request.prompt}
    if images.first_frame is not None:
        instance["image"] = _inline(images.first_frame)
    if images.last_frame is not None:
        instance["lastFrame"] = _inline(images.last_frame)
    if images.references:
        instance["referenceImages"] = [
            {"image": _inline(item), "referenceType": "asset"} for item in images.references
        ]
    parameters: dict[str, Any] = {
        "durationSeconds": request.duration_seconds,
        "aspectRatio": request.aspect_ratio,
        "resolution": request.resolution,
        "sampleCount": 1,
        "generateAudio": request.generate_audio,
        "personGeneration": request.person_generation,
    }
    if request.negative_prompt:
        parameters["negativePrompt"] = request.negative_prompt
    if request.seed is not None:
        parameters["seed"] = request.seed
    return {"instances": [instance], "parameters": parameters}


def _inline(image: VeoInputImage) -> dict[str, str]:
    return {
        "bytesBase64Encoded": base64.b64encode(image.content).decode("ascii"),
        "mimeType": image.media_type,
    }


class GoogleVeoProvider:
    """The configured Google Veo adapter.

    The adapter never reads a credential from the environment itself: a caller
    supplies ``access_token`` as a callable so the same class works against a
    real deployment, a mocked transport, and CI with no credentials at all.
    """

    def __init__(
        self,
        *,
        project: str,
        access_token: Callable[[], str],
        location: str = DEFAULT_VEO_LOCATION,
        model: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 60.0,
        max_polls: int = 90,
    ) -> None:
        self._profile = capability_profile(model)
        if location not in self._profile.regions:
            raise UnsupportedVeoCapability(
                f"unsupported_region: {location!r}; {self._profile.model} is documented in "
                + ", ".join(self._profile.regions)
            )
        self._project = project
        self._location = location
        self._access_token = access_token
        self._max_polls = max_polls
        self._client = httpx.AsyncClient(
            base_url=(
                f"https://{location}-aiplatform.googleapis.com/{VEO_API_VERSION}/"
                f"projects/{project}/locations/{location}/publishers/google/models"
            ),
            transport=transport,
            timeout=httpx.Timeout(timeout_seconds),
        )

    @property
    def name(self) -> str:
        return VEO_PROVIDER_NAME

    @property
    def model(self) -> str:
        return self._profile.model

    @property
    def capabilities(self) -> VeoCapabilityProfile:
        return self._profile

    async def __aenter__(self) -> GoogleVeoProvider:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        # The token is read per request and never stored, logged or persisted.
        return {
            "Authorization": f"Bearer {self._access_token()}",
            "Content-Type": "application/json; charset=utf-8",
            "x-goog-user-project": self._project,
        }

    async def submit(self, request: VeoGenerationRequest, images: VeoInputImages) -> str:
        validate_veo_request(request, self._profile)
        payload = veo_request_payload(request, images)
        try:
            response = await self._client.post(
                f"/{self._profile.model}:{VEO_SUBMIT_METHOD}",
                headers=self._headers(),
                json=payload,
            )
        except httpx.HTTPError as error:
            # We cannot tell whether Vertex AI accepted the request. Blind
            # resubmission is exactly how a project gets billed twice.
            raise VeoSubmissionAmbiguous(
                "veo submission outcome is unknown after a transport failure; "
                f"reconcile before resubmitting ({type(error).__name__})"
            ) from error
        _raise_for_status(response)
        try:
            name = str(json.loads(response.text)["name"])
        except (KeyError, TypeError, ValueError) as error:
            raise VeoSubmissionAmbiguous(
                "veo submission response carried no operation name"
            ) from error
        if not name:
            raise VeoSubmissionAmbiguous("veo submission response carried an empty operation name")
        return name

    async def poll(self, operation_name: str) -> VeoGenerationResult:
        started = datetime.now(UTC)
        response = await self._client.post(
            f"/{self._profile.model}:{VEO_POLL_METHOD}",
            headers=self._headers(),
            json={"operationName": operation_name},
        )
        _raise_for_status(response)
        payload = json.loads(response.text)
        latency_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
        return self._result(operation_name, payload, latency_ms=latency_ms)

    def _result(
        self, operation_name: str, payload: dict[str, Any], *, latency_ms: int
    ) -> VeoGenerationResult:
        key = _idempotency_key(operation_name)
        if not payload.get("done"):
            return VeoGenerationResult(
                application_idempotency_key=key,
                operation_name=operation_name,
                model=self._profile.model,
                state=VeoOperationState.RUNNING,
                latency_ms=latency_ms,
            )
        error = payload.get("error")
        if error:
            return VeoGenerationResult(
                application_idempotency_key=key,
                operation_name=operation_name,
                model=self._profile.model,
                state=VeoOperationState.FAILED,
                failure_code=str(error.get("code", "veo_operation_failed"))[:128],
                failure_message=str(error.get("message", ""))[:500],
                latency_ms=latency_ms,
            )
        videos = _videos(payload)
        filtered = int(payload.get("response", {}).get("raiMediaFilteredCount", 0) or 0)
        if not videos:
            return VeoGenerationResult(
                application_idempotency_key=key,
                operation_name=operation_name,
                model=self._profile.model,
                state=VeoOperationState.FAILED,
                failure_code="veo_safety_filtered" if filtered else "veo_empty_response",
                failure_message=(
                    "the provider filtered every candidate for its own safety policy"
                    if filtered
                    else "the operation completed with no video output"
                ),
                rai_filtered_count=min(filtered, 4),
                latency_ms=latency_ms,
            )
        return VeoGenerationResult(
            application_idempotency_key=key,
            operation_name=operation_name,
            model=self._profile.model,
            state=VeoOperationState.SUCCEEDED,
            output_count=min(len(videos), 4),
            rai_filtered_count=min(filtered, 4),
            has_audio=self._profile.generates_native_audio,
            latency_ms=latency_ms,
            redacted_metadata={"provider": VEO_PROVIDER_NAME, "model": self._profile.model},
        )

    async def download(self, operation_name: str, destination: Path) -> DownloadedMedia:
        response = await self._client.post(
            f"/{self._profile.model}:{VEO_POLL_METHOD}",
            headers=self._headers(),
            json={"operationName": operation_name},
        )
        _raise_for_status(response)
        videos = _videos(json.loads(response.text))
        if not videos:
            raise ValueError("veo_missing_output: the completed operation carries no video")
        video = videos[0]
        inline = video.get("bytesBase64Encoded")
        if inline is not None:
            return await _consume(destination, _decode_base64(str(inline)))
        uri = str(video.get("gcsUri") or video.get("uri") or "")
        if not uri:
            raise ValueError("veo_missing_output: no inline bytes and no download URI")
        if uri.startswith("gs://"):
            # T21 never sets ``storageUri``, so Veo returns inline bytes. A
            # Cloud Storage handle means the request was built elsewhere, and
            # this adapter has no Cloud Storage client to fetch it with.
            raise ValueError(
                "veo_unsupported_output_location: the operation wrote to Cloud Storage, "
                "which this adapter does not read; T21 requests inline output"
            )
        return await self._stream(uri, destination)

    async def _stream(self, uri: str, destination: Path) -> DownloadedMedia:
        # The URI itself is a short-lived credentialed handle and is never
        # logged, persisted or echoed into an error message.
        digest = hashlib.sha256()
        total = 0
        async with self._client.stream(
            "GET", uri, headers={"Authorization": f"Bearer {self._access_token()}"}
        ) as response:
            _raise_for_status(response)
            with destination.open("wb") as stream:
                async for chunk in response.aiter_bytes(DOWNLOAD_CHUNK_BYTES):
                    total += len(chunk)
                    if total > MAX_DOWNLOAD_BYTES:
                        raise ValueError("veo_output_too_large: refusing an unbounded download")
                    digest.update(chunk)
                    stream.write(chunk)
        if not total:
            raise ValueError("veo_incomplete_download: the provider returned an empty body")
        return DownloadedMedia(destination, digest.hexdigest(), total)


def _videos(payload: dict[str, Any]) -> list[dict[str, Any]]:
    response = payload.get("response") or {}
    videos = response.get("videos") or response.get("generatedSamples") or []
    return [item for item in videos if isinstance(item, dict)]


def _decode_base64(encoded: str) -> AsyncIterator[bytes]:
    async def chunks() -> AsyncIterator[bytes]:
        # Decode in bounded blocks so a large clip is never fully materialized
        # twice. Base64 decodes cleanly on any multiple of four characters.
        block = (DOWNLOAD_CHUNK_BYTES // 3) * 4
        for start in range(0, len(encoded), block):
            try:
                yield base64.b64decode(encoded[start : start + block], validate=True)
            except (binascii.Error, ValueError) as error:
                raise ValueError("veo_corrupt_output: inline video is not valid base64") from error

    return chunks()


async def _consume(destination: Path, chunks: AsyncIterator[bytes]) -> DownloadedMedia:
    digest = hashlib.sha256()
    total = 0
    with destination.open("wb") as stream:
        async for chunk in chunks:
            total += len(chunk)
            if total > MAX_DOWNLOAD_BYTES:
                raise ValueError("veo_output_too_large: refusing an unbounded download")
            digest.update(chunk)
            stream.write(chunk)
    if not total:
        raise ValueError("veo_incomplete_download: the provider returned an empty body")
    return DownloadedMedia(destination, digest.hexdigest(), total)


def _raise_for_status(response: httpx.Response) -> None:
    if response.status_code == 429:
        raise VeoRateLimited("veo_rate_limited: the provider asked us to slow down")
    if response.status_code in {408, 504}:
        raise VeoOperationTimeout("veo_gateway_timeout: the operation is still durable")
    if response.status_code >= 400:
        # Never echo a provider body: it can contain the prompt or a signed URL.
        raise httpx.HTTPStatusError(
            f"veo_http_{response.status_code}",
            request=response.request,
            response=response,
        )


def temporary_download_path(prefix: str = "vidgen-veo-") -> Path:
    handle = NamedTemporaryFile(prefix=prefix, suffix=".mp4", delete=False)
    handle.close()
    return Path(handle.name)


def _idempotency_key(operation_name: str) -> str:
    return hashlib.sha256(operation_name.encode()).hexdigest()[:64]


@dataclass(frozen=True, slots=True)
class VeoAdapterSettings:
    """The deployment configuration a real Veo adapter needs."""

    project: str
    location: str = DEFAULT_VEO_LOCATION
    model: str | None = None
    extra_headers: dict[str, str] = field(default_factory=dict)


__all__ = [
    "DOWNLOAD_CHUNK_BYTES",
    "MAX_DOWNLOAD_BYTES",
    "AlternateVideoProvider",
    "DownloadedMedia",
    "GoogleVeoProvider",
    "VeoAdapterSettings",
    "VeoInputImage",
    "VeoInputImages",
    "temporary_download_path",
    "validate_veo_request",
    "veo_request_payload",
]
