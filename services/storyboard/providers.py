"""Storyboard Director boundary and centralized visual-provider capability profiles.

The pipeline depends on this Protocol, never on an SDK response object. Model
names and provider configuration live here so no adapter or solver bakes in a
single vendor's limits.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Protocol

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
DIRECTOR_VERSION = "storyboard-director/1.0.0"
PROMPT_VERSION = "storyboard-director-v1"
FAKE_DIRECTOR_MODEL = "fake-storyboard-1"


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

#: Continuous-duration provider: Runway-style Gen-4 Turbo on 100 ms steps.
CONTINUOUS_PROFILE = build_capability_profile(
    capability_profile_id="runway-gen4-turbo",
    profile_version=1,
    provider="runway",
    model_family="gen4_turbo",
    supported_generation_durations_us=[],
    min_generation_duration_us=_seconds("1"),
    max_generation_duration_us=_seconds("10"),
    duration_increment_us=_seconds("0.1"),
    supported_aspect_ratios=["16:9", "9:16", "1:1"],
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
        "crane",
        "handheld",
        "zoom_in",
        "zoom_out",
    ],
    supported_transitions=["cut", "dissolve", "fade_in", "fade_out", "match_cut", "whip_pan"],
    supports_image_to_video=True,
    supports_text_to_video=False,
    supports_continuity_seed=True,
    trimming_policy="trim_end",
)

CAPABILITY_PROFILES: dict[str, VisualProviderCapability] = {
    DISCRETE_PROFILE.capability_profile_id: DISCRETE_PROFILE,
    CONTINUOUS_PROFILE.capability_profile_id: CONTINUOUS_PROFILE,
}
DEFAULT_CAPABILITY_PROFILE_ID = CONTINUOUS_PROFILE.capability_profile_id


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
