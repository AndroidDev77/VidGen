"""Verified Runway capability registry, derived profiles, pricing and adapter requests.

Every limit here is the one the official documentation and the Runway OpenAPI
request types state for ``gen4_turbo`` and ``gen4.5``; no paid call is made.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from services.animation.pricing import (
    CREDITS_PER_SECOND,
    PRICING_CONFIGURATION_VERSION,
    estimate_runway_cost,
    runway_pricing_catalog,
)
from services.animation.providers import (
    CAPABILITIES,
    GEN4_5_CAPABILITY,
    GEN4_TURBO_CAPABILITY,
    RUNWAY_IMAGE_TO_VIDEO_DIMENSIONS,
    capability_for,
    capability_registry_hash,
    validate_request,
)
from services.animation.runway import RunwayVideoProvider
from services.storyboard.providers import (
    CAPABILITY_PROFILES,
    RUNWAY_GEN4_5_PROFILE,
    RUNWAY_GEN4_TURBO_PROFILE,
    load_capability_profile,
)
from services.storyboard.retimer import select_generation_duration
from vidgen.contracts.animation import RunwayModel, VideoProvider, VideoProviderRequest

SECOND = 1_000_000


def request(model: RunwayModel, duration: float, width: int = 1280, height: int = 720):
    return VideoProviderRequest(
        application_idempotency_key="stable",
        project_id=uuid4(),
        animation_run_id=uuid4(),
        animation_item_id=uuid4(),
        storyboard_id=uuid4(),
        storyboard_version=1,
        shot_id=uuid4(),
        shot_sequence=0,
        first_keyframe_asset_id=uuid4(),
        first_keyframe_sha256="a" * 64,
        compiled_motion_prompt="subject turns once",
        provider=VideoProvider.RUNWAY,
        model=model,
        requested_duration_seconds=duration,
        width=width,
        height=height,
        attempt_number=1,
        provider_configuration_version="runway/2024-11-06",
    )


# -- the registry ------------------------------------------------------------


def test_the_registry_names_exactly_the_two_verified_models() -> None:
    assert set(CAPABILITIES) == {"gen4_turbo", "gen4.5"}
    assert capability_for(RunwayModel.GEN4_TURBO) is GEN4_TURBO_CAPABILITY
    assert capability_for("gen4.5") is GEN4_5_CAPABILITY
    with pytest.raises(ValueError, match="unknown Runway model"):
        capability_for("gen3a_turbo")


def test_gen4_turbo_accepts_whole_seconds_from_two_to_ten_and_not_one() -> None:
    assert GEN4_TURBO_CAPABILITY.min_duration_seconds == 2
    assert GEN4_TURBO_CAPABILITY.max_duration_seconds == 10
    assert GEN4_TURBO_CAPABILITY.durations == tuple(range(2, 11))
    validate_request(request(RunwayModel.GEN4_TURBO, 2))
    validate_request(request(RunwayModel.GEN4_TURBO, 10))
    with pytest.raises(ValueError, match="unsupported_duration"):
        validate_request(request(RunwayModel.GEN4_TURBO, 1))
    with pytest.raises(ValueError, match="unsupported_duration"):
        validate_request(request(RunwayModel.GEN4_TURBO, 2.5))
    with pytest.raises(ValueError, match="unsupported_duration"):
        validate_request(request(RunwayModel.GEN4_TURBO, 11))


def test_gen4_5_durations_above_ten_seconds_are_refused_until_the_api_accepts_them() -> None:
    # The Gen-4.5 image-to-video schema states "Must be an integer from 2 to
    # 10"; the registry is the single place to extend when that changes.
    assert GEN4_5_CAPABILITY.durations == tuple(range(2, 11))
    validate_request(request(RunwayModel.GEN4_5, 10))
    assert GEN4_5_CAPABILITY.select_duration(10) == 10
    assert GEN4_5_CAPABILITY.select_duration(10.5) is None
    with pytest.raises(ValueError, match="unsupported_duration"):
        validate_request(request(RunwayModel.GEN4_5, 12))


def test_gen4_5_requires_prompt_text_and_supports_text_to_video() -> None:
    assert GEN4_5_CAPABILITY.text_to_video is True
    assert GEN4_5_CAPABILITY.prompt_required is True
    assert GEN4_TURBO_CAPABILITY.text_to_video is False
    empty = request(RunwayModel.GEN4_5, 4).model_copy(update={"compiled_motion_prompt": " "})
    with pytest.raises(ValueError, match="requires prompt text"):
        validate_request(empty)


@pytest.mark.parametrize("model", [RunwayModel.GEN4_TURBO, RunwayModel.GEN4_5])
def test_portrait_ratios_are_accepted_for_both_models(model: RunwayModel) -> None:
    validate_request(request(model, 5, 720, 1280))
    validate_request(request(model, 5, 832, 1104))
    with pytest.raises(ValueError, match="unsupported_dimensions"):
        validate_request(request(model, 5, 1080, 1920))
    assert (720, 1280) in RUNWAY_IMAGE_TO_VIDEO_DIMENSIONS


def test_duration_selection_rounds_up_to_the_next_accepted_second() -> None:
    assert GEN4_TURBO_CAPABILITY.select_duration(3.2) == 4
    assert GEN4_TURBO_CAPABILITY.select_duration(1) == 2
    assert GEN4_TURBO_CAPABILITY.select_duration(10) == 10
    assert GEN4_TURBO_CAPABILITY.select_duration(10.01) is None


# -- derived storyboard profiles -------------------------------------------------


def test_storyboard_profiles_are_derived_from_the_registry_and_stay_consistent() -> None:
    for capability, profile in (
        (GEN4_TURBO_CAPABILITY, RUNWAY_GEN4_TURBO_PROFILE),
        (GEN4_5_CAPABILITY, RUNWAY_GEN4_5_PROFILE),
    ):
        assert profile.capability_profile_id == capability.capability_profile_id
        assert profile.min_generation_duration_us == capability.min_duration_seconds * SECOND
        assert profile.max_generation_duration_us == capability.max_duration_seconds * SECOND
        assert profile.supported_generation_durations_us == [
            second * SECOND for second in capability.durations
        ]
        assert "9:16" in profile.supported_aspect_ratios
        assert "720x1280" in profile.supported_resolutions
        assert profile.supports_text_to_video == capability.text_to_video
        # Whatever the storyboard plans is a duration the adapter accepts.
        for duration in profile.supported_generation_durations_us:
            assert capability.supports_duration(duration / SECOND)
    assert CAPABILITY_PROFILES["runway-gen4-turbo"] is RUNWAY_GEN4_TURBO_PROFILE
    assert load_capability_profile("runway-gen4.5") is RUNWAY_GEN4_5_PROFILE


def test_the_gen4_turbo_profile_no_longer_plans_sub_two_second_generations() -> None:
    assert select_generation_duration(500_000, RUNWAY_GEN4_TURBO_PROFILE) == 2 * SECOND
    assert select_generation_duration(3_310_000, RUNWAY_GEN4_TURBO_PROFILE) == 4 * SECOND
    assert select_generation_duration(10 * SECOND, RUNWAY_GEN4_TURBO_PROFILE) == 10 * SECOND
    assert select_generation_duration(10 * SECOND + 1, RUNWAY_GEN4_TURBO_PROFILE) is None
    assert RUNWAY_GEN4_TURBO_PROFILE.profile_version == 2


def test_capability_hashes_are_stable_and_content_bound() -> None:
    assert capability_registry_hash() == capability_registry_hash()
    assert len(capability_registry_hash()) == 64
    assert GEN4_TURBO_CAPABILITY.capability_hash() == GEN4_TURBO_CAPABILITY.capability_hash()
    assert GEN4_TURBO_CAPABILITY.capability_hash() != GEN4_5_CAPABILITY.capability_hash()
    assert RUNWAY_GEN4_TURBO_PROFILE.capability_hash != RUNWAY_GEN4_5_PROFILE.capability_hash
    assert (
        load_capability_profile(None).capability_hash == RUNWAY_GEN4_TURBO_PROFILE.capability_hash
    )


# -- pricing -------------------------------------------------------------------


def test_pricing_is_derived_from_the_registry_at_the_verified_rates() -> None:
    assert CREDITS_PER_SECOND == {"gen4_turbo": Decimal("5"), "gen4.5": Decimal("12")}
    assert estimate_runway_cost("gen4_turbo", 4) == Decimal("0.200000")
    assert estimate_runway_cost("gen4.5", 4) == Decimal("0.480000")
    # Whole seconds are billed, so a fractional request prices its rounded job.
    assert estimate_runway_cost("gen4_turbo", 3.5) == Decimal("0.200000")
    catalog = runway_pricing_catalog()
    assert catalog.name == PRICING_CONFIGURATION_VERSION
    assert {rate.model: rate.unit_price for rate in catalog.rates} == {
        "gen4_turbo": Decimal("0.05"),
        "gen4.5": Decimal("0.12"),
    }
    with pytest.raises(ValueError, match="unknown Runway pricing model"):
        estimate_runway_cost("gen3a_turbo", 4)


# -- the adapter, mocked ---------------------------------------------------------


class _Resource:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response

    async def retrieve(self, value):
        self.calls.append(value)
        return self.response


@pytest.mark.parametrize(
    ("model", "duration", "width", "height"),
    [(RunwayModel.GEN4_TURBO, 4, 1280, 720), (RunwayModel.GEN4_5, 9, 720, 1280)],
)
def test_the_adapter_submits_both_models_against_their_own_capability_profile(
    model: RunwayModel, duration: int, width: int, height: int
) -> None:
    created = _Resource(SimpleNamespace(id=f"task-{model.value}"))
    retrieved = _Resource(
        SimpleNamespace(
            status="THROTTLED",
            model=model.value,
            duration=duration,
            created_at=datetime.now(UTC),
            output=None,
        )
    )
    provider = RunwayVideoProvider(SimpleNamespace(image_to_video=created, tasks=retrieved))
    submitted = asyncio.run(
        provider.submit(request(model, duration, width, height), "data:image/png;base64,abc")
    )
    assert submitted.model is model
    assert created.calls == [
        {
            "model": model.value,
            "prompt_image": "data:image/png;base64,abc",
            "prompt_text": "subject turns once",
            "duration": duration,
            "ratio": f"{width}:{height}",
        }
    ]
    polled = asyncio.run(provider.retrieve(submitted.remote_task_id))
    # THROTTLED is Runway's queue state; it is polled exactly like PENDING.
    assert polled.status.value == "pending"
    assert polled.model is model


def test_the_adapter_never_sends_a_request_the_model_cannot_serve() -> None:
    created = _Resource(SimpleNamespace(id="never"))
    provider = RunwayVideoProvider(SimpleNamespace(image_to_video=created, tasks=created))
    with pytest.raises(ValueError, match="unsupported_duration"):
        asyncio.run(provider.submit(request(RunwayModel.GEN4_5, 11), "data:image/png;base64,x"))
    assert created.calls == []
