"""The single source of truth for Google Veo models, capabilities and pricing.

Every Veo fact this repository relies on lives here: the supported model IDs,
the fast and quality variants, durations, aspect ratios, resolutions, frame and
reference controls, native-audio behaviour, regional availability, request
limits, polling behaviour and the pricing identifiers T23 estimates from. No
other module may name a Veo model, and no module may assume a capability that
is not declared on the profile it was handed.

Capabilities are versioned. ``VEO_CAPABILITY_VERSION`` changes whenever a
declared capability changes, and every repair attempt persists the profile hash
it was generated under, so an attempt can always be replayed against the exact
capability set that produced it.

Verified against Google's published Vertex AI Veo model and pricing
documentation on the date recorded in ``CAPABILITY_VERIFICATION_DATE``. Re-check
that documentation before changing a model ID, a duration, or a price: Veo model
IDs and per-second prices have changed more than once, and a stale value here
silently mis-estimates every alternate-provider repair.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from vidgen.contracts.costs import PricingCatalogVersion, PricingRate
from vidgen.contracts.telemetry import UsageUnit

VEO_PROVIDER_NAME = "google_veo"
VEO_CAPABILITY_VERSION = "veo-capabilities/2026-08-28"
VEO_PRICING_CONFIGURATION_VERSION = "veo-pricing-2026-08-28"
VEO_OPERATION_NAMESPACE = "google-veo-operation"
CAPABILITY_SOURCE = "https://cloud.google.com/vertex-ai/generative-ai/docs/models/veo"
PRICING_SOURCE = "https://cloud.google.com/vertex-ai/generative-ai/pricing"
CAPABILITY_VERIFICATION_DATE = date(2026, 8, 28)

#: Vertex AI publishes Veo generation as a long-running operation: ``predictLongRunning``
#: returns an operation name, and ``fetchPredictOperation`` is polled until ``done``.
VEO_SUBMIT_METHOD = "predictLongRunning"
VEO_POLL_METHOD = "fetchPredictOperation"
VEO_API_VERSION = "v1"
DEFAULT_VEO_LOCATION = "us-central1"
#: Regions Google documents for Veo generation. A deployment outside this set is
#: a configuration error, not a runtime fallback.
VEO_REGIONS: tuple[str, ...] = ("us-central1", "us-east4", "europe-west4", "asia-northeast1")


@dataclass(frozen=True, slots=True)
class VeoCapabilityProfile:
    """One versioned, immutable declaration of what one Veo model can do."""

    model: str
    variant: Literal["fast", "quality"]
    durations_seconds: tuple[int, ...]
    aspect_ratios: tuple[str, ...]
    resolutions: tuple[str, ...]
    supports_image_to_video: bool
    supports_first_frame: bool
    supports_last_frame: bool
    supports_reference_images: bool
    max_reference_images: int
    #: Veo 3 models generate native audio. T17 owns the final mix, so T21 asks
    #: for silent video wherever the model lets it, and discards any audio track
    #: the model returns anyway.
    generates_native_audio: bool
    audio_is_optional: bool
    max_prompt_characters: int
    max_concurrent_operations: int
    poll_interval_seconds: float
    poll_timeout_seconds: float
    regions: tuple[str, ...] = VEO_REGIONS
    capability_version: str = VEO_CAPABILITY_VERSION
    source_reference: str = CAPABILITY_SOURCE
    verification_date: date = CAPABILITY_VERIFICATION_DATE
    notes: str = ""

    def material(self) -> dict[str, object]:
        """Exactly the fields bound into the capability-profile hash."""
        return {
            "capability_version": self.capability_version,
            "model": self.model,
            "variant": self.variant,
            "durations_seconds": list(self.durations_seconds),
            "aspect_ratios": list(self.aspect_ratios),
            "resolutions": list(self.resolutions),
            "supports_image_to_video": self.supports_image_to_video,
            "supports_first_frame": self.supports_first_frame,
            "supports_last_frame": self.supports_last_frame,
            "supports_reference_images": self.supports_reference_images,
            "max_reference_images": self.max_reference_images,
            "generates_native_audio": self.generates_native_audio,
            "audio_is_optional": self.audio_is_optional,
            "max_prompt_characters": self.max_prompt_characters,
            "max_concurrent_operations": self.max_concurrent_operations,
            "regions": list(self.regions),
        }

    @property
    def profile_hash(self) -> str:
        payload = json.dumps(self.material(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()

    def smallest_supported_duration(self, requested_seconds: float) -> int:
        """The shortest offered clip that still covers the requested duration.

        Veo offers a small set of whole-second durations, so a shot whose exact
        canonical length is not one of them is generated longer and trimmed
        deterministically by the existing T15 trimmer.
        """
        for duration in self.durations_seconds:
            if duration + 1e-9 >= requested_seconds:
                return duration
        raise UnsupportedVeoCapability(
            f"unsupported_duration: {self.model} generates at most "
            f"{max(self.durations_seconds)}s, {requested_seconds:.3f}s requested"
        )

    def aspect_ratio_for(self, width: int, height: int) -> str:
        ratio = _aspect_ratio(width, height)
        if ratio not in self.aspect_ratios:
            raise UnsupportedVeoCapability(
                f"unsupported_aspect_ratio: {self.model} supports "
                f"{', '.join(self.aspect_ratios)}, {ratio} requested"
            )
        return ratio

    def resolution_for(self, height: int) -> str:
        resolution = "1080p" if height >= 1080 else "720p"
        if resolution not in self.resolutions:
            raise UnsupportedVeoCapability(
                f"unsupported_resolution: {self.model} supports "
                f"{', '.join(self.resolutions)}, {resolution} requested"
            )
        return resolution


class UnsupportedVeoCapability(ValueError):
    """The request asks a Veo model for something its profile does not declare."""


class VeoSubmissionAmbiguous(RuntimeError):
    """A submission may or may not have started a paid operation.

    Never resolved by resubmitting. The checkpoint is marked ambiguous and the
    shot is routed to human review so a person reconciles the provider side.
    """


class VeoOperationTimeout(TimeoutError):
    """The polling window expired while a durable operation is still running.

    Retryable, and explicitly *not* a new generation: the operation name is
    already persisted, so the next worker resumes the same operation.
    """


class VeoRateLimited(RuntimeError):
    """The provider asked us to slow down. Retryable, never a resubmission."""


#: Veo 3.1 is the current generation. The fast variant is the cost-controlled
#: default for T21 fallback; the quality variant exists so a deployment can opt
#: in, never so the router can silently escalate spend.
VEO_31_FAST = VeoCapabilityProfile(
    model="veo-3.1-fast-generate-001",
    variant="fast",
    durations_seconds=(4, 6, 8),
    aspect_ratios=("16:9", "9:16"),
    resolutions=("720p", "1080p"),
    supports_image_to_video=True,
    supports_first_frame=True,
    supports_last_frame=True,
    supports_reference_images=True,
    max_reference_images=3,
    generates_native_audio=True,
    audio_is_optional=True,
    max_prompt_characters=4000,
    max_concurrent_operations=4,
    poll_interval_seconds=10.0,
    poll_timeout_seconds=900.0,
    notes="Cost-controlled fast variant; the T21 alternate-provider default.",
)

VEO_31_QUALITY = VeoCapabilityProfile(
    model="veo-3.1-generate-001",
    variant="quality",
    durations_seconds=(4, 6, 8),
    aspect_ratios=("16:9", "9:16"),
    resolutions=("720p", "1080p"),
    supports_image_to_video=True,
    supports_first_frame=True,
    supports_last_frame=True,
    supports_reference_images=True,
    max_reference_images=3,
    generates_native_audio=True,
    audio_is_optional=True,
    max_prompt_characters=4000,
    max_concurrent_operations=2,
    poll_interval_seconds=10.0,
    poll_timeout_seconds=900.0,
    notes="Quality variant; opt-in only, roughly 2.7x the fast per-second price.",
)

#: Veo 3.0 remains available and is declared so a deployment pinned to it keeps a
#: truthful profile. It has no last-frame or reference-image control, which is
#: exactly why capability enforcement reads the profile rather than the family.
VEO_30_FAST = VeoCapabilityProfile(
    model="veo-3.0-fast-generate-001",
    variant="fast",
    durations_seconds=(4, 6, 8),
    aspect_ratios=("16:9", "9:16"),
    resolutions=("720p", "1080p"),
    supports_image_to_video=True,
    supports_first_frame=True,
    supports_last_frame=False,
    supports_reference_images=False,
    max_reference_images=0,
    generates_native_audio=True,
    audio_is_optional=True,
    max_prompt_characters=4000,
    max_concurrent_operations=4,
    poll_interval_seconds=10.0,
    poll_timeout_seconds=900.0,
    notes="Previous generation; no last-frame or reference-image control.",
)

VEO_CAPABILITIES: dict[str, VeoCapabilityProfile] = {
    profile.model: profile for profile in (VEO_31_FAST, VEO_31_QUALITY, VEO_30_FAST)
}

#: The cost-controlled model T21 uses unless a deployment configures another.
DEFAULT_VEO_MODEL = VEO_31_FAST.model

#: USD per generated video second, from Google's published Vertex AI pricing.
VEO_USD_PER_SECOND: dict[str, Decimal] = {
    VEO_31_FAST.model: Decimal("0.15"),
    VEO_31_QUALITY.model: Decimal("0.40"),
    VEO_30_FAST.model: Decimal("0.15"),
}


def capability_profile(model: str | None = None) -> VeoCapabilityProfile:
    """Return the versioned profile for one Veo model.

    An unknown model is a configuration error rather than a best-effort guess:
    assuming an undeclared capability is exactly how a repair attempt silently
    drops a required last frame.
    """
    resolved = model or DEFAULT_VEO_MODEL
    try:
        return VEO_CAPABILITIES[resolved]
    except KeyError as error:
        supported = ", ".join(sorted(VEO_CAPABILITIES))
        raise UnsupportedVeoCapability(
            f"unknown_veo_model: {resolved!r}; supported models are {supported}"
        ) from error


def veo_pricing_catalog() -> PricingCatalogVersion:
    """The immutable T23 catalog projection used for Veo repair estimates."""
    version_id = uuid5(NAMESPACE_URL, VEO_PRICING_CONFIGURATION_VERSION)
    effective = datetime(2026, 8, 28, tzinfo=UTC)
    rates = tuple(
        PricingRate(
            pricing_version_id=version_id,
            provider=VEO_PROVIDER_NAME,
            model=model,
            operation="video_generation",
            usage_unit=UsageUnit.VIDEO_OUTPUT_SECOND,
            unit_size=Decimal("1"),
            unit_price=price,
            currency="USD",
            effective_start=effective,
            source_reference=PRICING_SOURCE,
            verification_date=CAPABILITY_VERIFICATION_DATE,
            notes=(f"${price}/generated second; configuration={VEO_PRICING_CONFIGURATION_VERSION}"),
        )
        for model, price in sorted(VEO_USD_PER_SECOND.items())
    )
    return PricingCatalogVersion(
        id=version_id, name=VEO_PRICING_CONFIGURATION_VERSION, currency="USD", rates=rates
    )


def estimate_veo_cost(model: str, duration_seconds: float) -> Decimal:
    """Estimate one Veo generation from the active pricing identifiers.

    Veo bills the *generated* duration, so the estimate uses the duration the
    adapter will actually request - the smallest supported clip covering the
    shot - not the shorter canonical duration the trimmer produces.
    """
    try:
        price = VEO_USD_PER_SECOND[model]
    except KeyError as error:
        raise UnsupportedVeoCapability(f"unknown Veo pricing model: {model}") from error
    if duration_seconds <= 0:
        raise ValueError("a Veo generation estimate needs a positive duration")
    return (price * Decimal(str(duration_seconds))).quantize(Decimal("0.000001"))


@dataclass(frozen=True, slots=True)
class VeoRequestLimits:
    """Bounded request shape enforced before any paid call."""

    max_prompt_characters: int
    max_reference_images: int
    max_input_image_bytes: int = 20 * 1024 * 1024
    max_concurrent_operations: int = 4
    supported_input_media: tuple[str, ...] = field(
        default=("image/png", "image/jpeg", "image/webp")
    )


def request_limits(profile: VeoCapabilityProfile) -> VeoRequestLimits:
    return VeoRequestLimits(
        max_prompt_characters=profile.max_prompt_characters,
        max_reference_images=profile.max_reference_images,
        max_concurrent_operations=profile.max_concurrent_operations,
    )


def _aspect_ratio(width: int, height: int) -> str:
    if width <= 0 or height <= 0:
        raise UnsupportedVeoCapability("a Veo request needs positive dimensions")
    divisor = _gcd(width, height)
    return f"{width // divisor}:{height // divisor}"


def _gcd(left: int, right: int) -> int:
    while right:
        left, right = right, left % right
    return left


__all__ = [
    "CAPABILITY_SOURCE",
    "CAPABILITY_VERIFICATION_DATE",
    "DEFAULT_VEO_LOCATION",
    "DEFAULT_VEO_MODEL",
    "PRICING_SOURCE",
    "VEO_30_FAST",
    "VEO_31_FAST",
    "VEO_31_QUALITY",
    "VEO_API_VERSION",
    "VEO_CAPABILITIES",
    "VEO_CAPABILITY_VERSION",
    "VEO_POLL_METHOD",
    "VEO_PRICING_CONFIGURATION_VERSION",
    "VEO_PROVIDER_NAME",
    "VEO_REGIONS",
    "VEO_SUBMIT_METHOD",
    "VEO_USD_PER_SECOND",
    "UnsupportedVeoCapability",
    "VeoCapabilityProfile",
    "VeoOperationTimeout",
    "VeoRateLimited",
    "VeoRequestLimits",
    "VeoSubmissionAmbiguous",
    "capability_profile",
    "estimate_veo_cost",
    "request_limits",
    "veo_pricing_catalog",
]
