"""Provider-neutral asynchronous video boundary and the centralized Runway registry.

Every Runway limit VidGen depends on lives in this module and nowhere else: the
model identifiers, accepted generated durations, accepted output ratios, input
image limits, output formats, and the per-second credit price each model bills.
The storyboard capability profiles (``services.storyboard.providers``), the
pricing catalog (``services.animation.pricing``), request validation, the
routing policy, and the pre-workflow cost estimate all derive from this table.

Verified on 2026-09-09 against the official sources named in
``CAPABILITY_SOURCES``:

* ``gen4_turbo`` and ``gen4.5`` are the exact model identifiers. Gen-4 Turbo is
  image-to-video only; Gen-4.5 accepts text or image input.
* Both models accept an integer ``duration`` in whole seconds from 2 to 10
  (the Gen-4.5 image-to-video schema states "Must be an integer from 2 to 10").
  The one-second minimum and the 100 ms granularity an earlier profile assumed
  are not accepted by the API. Nothing longer than ten seconds is accepted for
  either model today; ``durations`` is the single place to extend when Runway
  raises the limit.
* Image-to-video ``ratio`` values for both models: ``1280:720``, ``720:1280``,
  ``1104:832``, ``832:1104``, ``960:960`` and ``1584:672``. Gen-4.5
  text-to-video accepts ``1280:720`` and ``720:1280``.
* ``promptText`` is at most 1000 UTF-16 code units and is required for Gen-4.5.
* ``promptImage`` is an HTTPS URL (16 MB), a Runway upload URI, or a base64
  data URI of at most 5 MB, in JPEG, PNG or WebP. The Gen-4.5 image-to-video
  input aspect ratio must fall between 0.5 and 2.0.
* Output defaults to an H.264 ``mp4``; Gen-4.5 also offers ProRes, PNG sequence
  and HDR formats at a per-second surcharge, none of which VidGen requests.
* Pricing: one credit is $0.01; ``gen4_turbo`` bills 5 credits per generated
  second and ``gen4.5`` bills 12.
* Tasks move through ``PENDING`` / ``THROTTLED`` / ``RUNNING`` and end in
  ``SUCCEEDED``, ``FAILED`` or ``CANCELLED``. ``THROTTLED`` means the
  organisation's concurrency limit for that model is reached and the task waits
  in Runway's queue; it is polled exactly like ``PENDING``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from math import ceil
from typing import Protocol

from vidgen.contracts.animation import RunwayModel, VideoProviderRequest, VideoProviderTask

RUNWAY_API_VERSION = "2024-11-06"
CAPABILITY_REGISTRY_VERSION = "runway-capabilities/2026-09-09"
CAPABILITY_VERIFICATION_DATE = date(2026, 9, 9)
CAPABILITY_SOURCES: tuple[str, ...] = (
    "https://docs.dev.runwayml.com/guides/pricing/",
    "https://docs.dev.runwayml.com/guides/models/",
    "https://docs.dev.runwayml.com/assets/inputs/",
    "runwayml Python SDK 5.20.0 request types generated from the Runway OpenAPI spec",
)
CREDIT_USD = Decimal("0.01")
#: Data URIs are capped at 5 MB once encoded; URLs may be 16 MB.
MAX_DATA_URI_BYTES = 5 * 1024 * 1024
MAX_INPUT_URL_BYTES = 16 * 1024 * 1024
SUPPORTED_INPUT_MEDIA_TYPES: tuple[str, ...] = ("image/png", "image/jpeg", "image/webp")
#: Every whole-second duration both current models accept for image-to-video.
RUNWAY_DURATIONS_SECONDS: tuple[int, ...] = tuple(range(2, 11))
#: Every image-to-video output ratio both current models accept. Portrait
#: (``720:1280``, ``832:1104``) is what VidGen needs for vertical recaps.
RUNWAY_IMAGE_TO_VIDEO_DIMENSIONS: tuple[tuple[int, int], ...] = (
    (1280, 720),
    (720, 1280),
    (1104, 832),
    (832, 1104),
    (960, 960),
    (1584, 672),
)
RUNWAY_TEXT_TO_VIDEO_DIMENSIONS: tuple[tuple[int, int], ...] = ((1280, 720), (720, 1280))
#: The conventional aspect-ratio name of each output ratio, for the T13 profile.
RUNWAY_ASPECT_LABELS: dict[tuple[int, int], str] = {
    (1280, 720): "16:9",
    (720, 1280): "9:16",
    (1104, 832): "4:3",
    (832, 1104): "3:4",
    (960, 960): "1:1",
    (1584, 672): "21:9",
}


@dataclass(frozen=True, slots=True)
class VideoCapability:
    """What one Runway model can actually generate. Immutable and hashable."""

    model: str
    display_name: str
    #: The T13 capability profile identifier derived from this model.
    capability_profile_id: str
    profile_version: int
    durations: tuple[int, ...]
    dimensions: tuple[tuple[int, int], ...]
    credits_per_second: int
    text_to_video_dimensions: tuple[tuple[int, int], ...] = ()
    prompt_characters: int = 1000
    prompt_required: bool = False
    image_to_video: bool = True
    text_to_video: bool = False
    supports_last_frame: bool = False
    formats: tuple[str, ...] = ("mp4",)
    max_input_bytes: int = MAX_DATA_URI_BYTES
    max_input_url_bytes: int = MAX_INPUT_URL_BYTES
    input_media_types: tuple[str, ...] = SUPPORTED_INPUT_MEDIA_TYPES
    #: Accepted input-image aspect ratio (width / height), inclusive.
    input_aspect_ratio_range: tuple[Decimal, Decimal] = (Decimal("0.5"), Decimal("2.0"))

    @property
    def min_duration_seconds(self) -> int:
        return min(self.durations)

    @property
    def max_duration_seconds(self) -> int:
        return max(self.durations)

    @property
    def unit_price_usd(self) -> Decimal:
        return Decimal(self.credits_per_second) * CREDIT_USD

    def supports_duration(self, seconds: float) -> bool:
        return float(seconds).is_integer() and int(seconds) in self.durations

    def select_duration(self, seconds: float) -> int | None:
        """The smallest accepted whole-second duration covering ``seconds``."""
        needed = max(1, ceil(Decimal(str(seconds))))
        return next((item for item in sorted(self.durations) if item >= needed), None)

    def supports_dimensions(self, width: int, height: int) -> bool:
        return (width, height) in self.dimensions

    def supports_input_aspect(self, width: int, height: int) -> bool:
        low, high = self.input_aspect_ratio_range
        ratio = Decimal(width) / Decimal(height)
        return low <= ratio <= high

    def material(self) -> dict[str, object]:
        """Everything a capability hash binds, in stable JSON-ready form."""
        return {
            "registry_version": CAPABILITY_REGISTRY_VERSION,
            "model": self.model,
            "capability_profile_id": self.capability_profile_id,
            "profile_version": self.profile_version,
            "durations": list(self.durations),
            "dimensions": [list(item) for item in self.dimensions],
            "text_to_video_dimensions": [list(item) for item in self.text_to_video_dimensions],
            "credits_per_second": self.credits_per_second,
            "prompt_characters": self.prompt_characters,
            "prompt_required": self.prompt_required,
            "image_to_video": self.image_to_video,
            "text_to_video": self.text_to_video,
            "supports_last_frame": self.supports_last_frame,
            "formats": list(self.formats),
            "max_input_bytes": self.max_input_bytes,
            "max_input_url_bytes": self.max_input_url_bytes,
            "input_media_types": list(self.input_media_types),
            "input_aspect_ratio_range": [str(item) for item in self.input_aspect_ratio_range],
        }

    def capability_hash(self) -> str:
        return hashlib.sha256(
            json.dumps(self.material(), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


GEN4_TURBO_CAPABILITY = VideoCapability(
    model=RunwayModel.GEN4_TURBO.value,
    display_name="Gen-4 Turbo",
    capability_profile_id="runway-gen4-turbo",
    profile_version=2,
    durations=RUNWAY_DURATIONS_SECONDS,
    dimensions=RUNWAY_IMAGE_TO_VIDEO_DIMENSIONS,
    credits_per_second=5,
    image_to_video=True,
    text_to_video=False,
)

GEN4_5_CAPABILITY = VideoCapability(
    model=RunwayModel.GEN4_5.value,
    display_name="Gen-4.5",
    capability_profile_id="runway-gen4.5",
    profile_version=1,
    durations=RUNWAY_DURATIONS_SECONDS,
    dimensions=RUNWAY_IMAGE_TO_VIDEO_DIMENSIONS,
    text_to_video_dimensions=RUNWAY_TEXT_TO_VIDEO_DIMENSIONS,
    credits_per_second=12,
    prompt_required=True,
    image_to_video=True,
    text_to_video=True,
)

CAPABILITIES: dict[str, VideoCapability] = {
    GEN4_TURBO_CAPABILITY.model: GEN4_TURBO_CAPABILITY,
    GEN4_5_CAPABILITY.model: GEN4_5_CAPABILITY,
}


def capability_for(model: RunwayModel | str) -> VideoCapability:
    key = model.value if isinstance(model, RunwayModel) else str(model)
    try:
        return CAPABILITIES[key]
    except KeyError as error:
        raise ValueError(f"unknown Runway model: {key}") from error


def capability_for_profile(capability_profile_id: str) -> VideoCapability | None:
    return next(
        (
            item
            for item in CAPABILITIES.values()
            if item.capability_profile_id == capability_profile_id
        ),
        None,
    )


def capability_registry_hash() -> str:
    """One hash over every model's capability material, for identity binding."""
    material = {
        "registry_version": CAPABILITY_REGISTRY_VERSION,
        "models": {key: value.material() for key, value in sorted(CAPABILITIES.items())},
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def validate_request(request: VideoProviderRequest) -> None:
    """Refuse a request the selected model's documented capabilities reject.

    Every failure here is a deterministic configuration failure: the request is
    never sent, no attempt is consumed, and nothing is retried.
    """
    capability = capability_for(request.model)
    if not capability.image_to_video:
        raise ValueError(f"unsupported_image_to_video: {request.model.value}")
    if not capability.supports_duration(request.requested_duration_seconds):
        raise ValueError(
            f"unsupported_duration: {request.requested_duration_seconds}; "
            f"{capability.display_name} accepts whole seconds from "
            f"{capability.min_duration_seconds} to {capability.max_duration_seconds}"
        )
    if not capability.supports_dimensions(request.width, request.height):
        raise ValueError(f"unsupported_dimensions: {request.width}:{request.height}")
    if len(request.compiled_motion_prompt) > capability.prompt_characters:
        raise ValueError("invalid_motion_prompt: provider prompt limit exceeded")
    if capability.prompt_required and not request.compiled_motion_prompt.strip():
        raise ValueError(f"invalid_motion_prompt: {request.model.value} requires prompt text")
    if request.output_format.value not in capability.formats:
        raise ValueError(f"unsupported_output_format: {request.output_format.value}")
    if request.last_keyframe_asset_id and not capability.supports_last_frame:
        raise ValueError(f"unsupported_strict_last_frame: {request.model.value}")


class VideoGenerationProvider(Protocol):
    name: str

    async def submit(
        self, request: VideoProviderRequest, prompt_image: str
    ) -> VideoProviderTask: ...
    async def retrieve(self, remote_task_id: str) -> VideoProviderTask: ...
    async def cancel(self, remote_task_id: str) -> bool: ...
