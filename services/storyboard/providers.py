"""Storyboard Director boundary and the visual-provider capability profiles T13 plans against.

The Runway profiles are *derived* from the single capability registry in
``services.animation.providers``: the durations, ratios and formats the retimer
plans against are the same values the Runway adapter validates every request
with, so the storyboard can never plan a duration the selected model cannot
generate. The pipeline depends on the Protocol below, never on an SDK response
object; model names and provider configuration live here so no adapter or
solver bakes in a single vendor's limits.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Protocol

from services.animation.providers import CAPABILITIES, RUNWAY_ASPECT_LABELS, VideoCapability
from services.storyboard.canonicalize import capability_profile_hash
from vidgen.contracts.storyboard import (
    MICROSECONDS_PER_SECOND,
    StoryboardProviderRequest,
    StoryboardProviderResult,
    VisualProviderCapability,
)

OPENAI_RESPONSES_PATH = "/responses"
OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_STORYBOARD_MODEL = "gpt-5.6"
DIRECTOR_VERSION = "storyboard-director/1.1.0"
#: v2 adds the shot-pacing guidance and the semantic cutting rules.
PROMPT_VERSION = "storyboard-director-v2"
FAKE_DIRECTOR_MODEL = "fake-storyboard-1"

#: Every camera movement and transition the Runway motion-prompt compiler can
#: express. Shared by both Runway profiles so they stay consistent.
RUNWAY_CAMERA_MOVEMENTS = [
    "static",
    "pan_left",
    "pan_right",
    "tilt_up",
    "tilt_down",
    "dolly_in",
    "dolly_out",
    "tracking",
    "crane",
    "handheld",
    "zoom_in",
    "zoom_out",
]
RUNWAY_TRANSITIONS = ["cut", "dissolve", "fade_in", "fade_out", "match_cut", "whip_pan"]


class StoryboardDirector(Protocol):
    """Creative shot proposal only. The retimer owns final timing."""

    name: str
    model: str

    async def propose(self, request: StoryboardProviderRequest) -> StoryboardProviderResult: ...


def build_capability_profile(**fields: Any) -> VisualProviderCapability:
    """Construct a capability profile with its tamper-evident hash filled in."""
    material = dict(fields)
    material.pop("capability_hash", None)
    probe = VisualProviderCapability.model_construct(**material)
    normalized = {
        key: value
        for key, value in probe.model_dump(mode="json").items()
        if key != "capability_hash"
    }
    return VisualProviderCapability(**material, capability_hash=capability_profile_hash(normalized))


def _seconds(value: str) -> int:
    """Exact microseconds from a decimal literal, never a binary float."""
    return int(Decimal(value) * MICROSECONDS_PER_SECOND)


def _aspect_ratio(width: int, height: int) -> str:
    from math import gcd

    label = RUNWAY_ASPECT_LABELS.get((width, height))
    if label is not None:
        return label
    divisor = gcd(width, height)
    return f"{width // divisor}:{height // divisor}"


def runway_capability_profile(capability: VideoCapability) -> VisualProviderCapability:
    """Project one registry entry into the provider-neutral T13 profile."""
    durations = [second * MICROSECONDS_PER_SECOND for second in sorted(capability.durations)]
    ratios: list[str] = []
    for width, height in capability.dimensions:
        ratio = _aspect_ratio(width, height)
        if ratio not in ratios:
            ratios.append(ratio)
    return build_capability_profile(
        capability_profile_id=capability.capability_profile_id,
        profile_version=capability.profile_version,
        provider="runway",
        model_family=capability.model,
        supported_generation_durations_us=durations,
        min_generation_duration_us=durations[0],
        max_generation_duration_us=durations[-1],
        duration_increment_us=MICROSECONDS_PER_SECOND,
        supported_aspect_ratios=ratios,
        supported_resolutions=[f"{width}x{height}" for width, height in capability.dimensions],
        max_characters_per_shot=3,
        max_reference_images=3,
        supports_camera_motion=True,
        supported_camera_movements=list(RUNWAY_CAMERA_MOVEMENTS),
        supported_transitions=list(RUNWAY_TRANSITIONS),
        supports_image_to_video=capability.image_to_video,
        supports_text_to_video=capability.text_to_video,
        supports_continuity_seed=True,
        trimming_policy="trim_end",
    )


#: Discrete-duration provider: Veo-style {4, 6, 8} second generations.
DISCRETE_PROFILE = build_capability_profile(
    capability_profile_id="veo-3.1-fast",
    profile_version=1,
    provider="vertex",
    model_family="veo-3.1-fast",
    supported_generation_durations_us=[_seconds("4"), _seconds("6"), _seconds("8")],
    min_generation_duration_us=_seconds("4"),
    max_generation_duration_us=_seconds("8"),
    duration_increment_us=_seconds("2"),
    supported_aspect_ratios=["16:9", "9:16"],
    supported_resolutions=["1920x1080", "1280x720"],
    max_characters_per_shot=3,
    max_reference_images=3,
    supports_camera_motion=True,
    supported_camera_movements=[
        "static",
        "pan_left",
        "pan_right",
        "tilt_up",
        "tilt_down",
        "dolly_in",
        "dolly_out",
        "tracking",
        "zoom_in",
        "zoom_out",
    ],
    supported_transitions=["cut", "dissolve", "fade_in", "fade_out", "match_cut"],
    supports_image_to_video=True,
    supports_text_to_video=True,
    supports_continuity_seed=True,
    trimming_policy="trim_end",
)

#: Gen-4 Turbo: whole seconds from 2 to 10, image-to-video only.
RUNWAY_GEN4_TURBO_PROFILE = runway_capability_profile(CAPABILITIES["gen4_turbo"])
#: Gen-4.5: the same generated durations and ratios, plus text-to-video.
RUNWAY_GEN4_5_PROFILE = runway_capability_profile(CAPABILITIES["gen4.5"])

#: A continuous-duration reference profile (100 ms steps from 1 s). It is not a
#: real provider and is not registered for projects; it exists so the solver's
#: continuous shape stays exercised by tests.
CONTINUOUS_PROFILE = build_capability_profile(
    capability_profile_id="continuous-reference",
    profile_version=1,
    provider="reference",
    model_family="continuous-reference",
    supported_generation_durations_us=[],
    min_generation_duration_us=_seconds("1"),
    max_generation_duration_us=_seconds("10"),
    duration_increment_us=_seconds("0.1"),
    supported_aspect_ratios=["16:9", "9:16", "1:1"],
    supported_resolutions=["1920x1080", "1280x720"],
    max_characters_per_shot=3,
    max_reference_images=3,
    supports_camera_motion=True,
    supported_camera_movements=list(RUNWAY_CAMERA_MOVEMENTS),
    supported_transitions=list(RUNWAY_TRANSITIONS),
    supports_image_to_video=True,
    supports_text_to_video=False,
    supports_continuity_seed=True,
    trimming_policy="trim_end",
)

CAPABILITY_PROFILES: dict[str, VisualProviderCapability] = {
    DISCRETE_PROFILE.capability_profile_id: DISCRETE_PROFILE,
    RUNWAY_GEN4_TURBO_PROFILE.capability_profile_id: RUNWAY_GEN4_TURBO_PROFILE,
    RUNWAY_GEN4_5_PROFILE.capability_profile_id: RUNWAY_GEN4_5_PROFILE,
}
DEFAULT_CAPABILITY_PROFILE_ID = RUNWAY_GEN4_TURBO_PROFILE.capability_profile_id


class CapabilityProfileError(ValueError):
    """A deterministic configuration failure. Never retried against a provider."""


def load_capability_profile(
    profile_id: str | None, overrides: dict[str, Any] | None = None
) -> VisualProviderCapability:
    """Resolve a configured capability profile, optionally overridden per project."""
    if overrides:
        material = {key: value for key, value in overrides.items() if key != "capability_hash"}
        try:
            return build_capability_profile(**material)
        except (TypeError, ValueError) as error:
            raise CapabilityProfileError(
                f"invalid visual-provider capability override: {error}"
            ) from error
    resolved = profile_id or DEFAULT_CAPABILITY_PROFILE_ID
    profile = CAPABILITY_PROFILES.get(resolved)
    if profile is None:
        raise CapabilityProfileError(
            f"unknown visual-provider capability profile {resolved!r}; configured profiles are "
            + ", ".join(sorted(CAPABILITY_PROFILES))
        )
    return profile
