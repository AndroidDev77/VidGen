from uuid import uuid4
import pytest
from services.animation.fake_provider import FakeVideoProvider
from services.animation.motion_prompt import compile_motion_prompt
from services.animation.routing import RoutingContext, route_model
from services.animation.task_poller import PollingWindowExpired, poll_task
from vidgen.contracts.animation import (
    MotionIntent,
    RunwayModel,
    VideoFormat,
    VideoProvider,
    VideoProviderRequest,
)


def intent():
    return MotionIntent(
        shot_id=uuid4(),
        shot_sequence=0,
        visual_purpose="reaction",
        primary_action="the subject raises one eyebrow",
        start_pose="neutral",
        expected_end_pose="eyebrow raised",
        camera_movement="locked camera",
        motion_intensity="low",
        continuity_invariants=[
            "identity, face, skin tone, hair, clothing, proportions, props, palette, and geometry remain unchanged"
        ],
    )


def request():
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
        compiled_motion_prompt="blink once",
        provider=VideoProvider.FAKE,
        model=RunwayModel.GEN4_TURBO,
        requested_duration_seconds=2,
        width=1280,
        height=720,
        output_format=VideoFormat.MP4,
        attempt_number=1,
        provider_configuration_version="v1",
    )


def test_prompt_and_routing_are_stable():
    item = intent()
    assert compile_motion_prompt(item) == compile_motion_prompt(item)
    assert route_model(RoutingContext()) == RunwayModel.GEN4_TURBO
    assert route_model(RoutingContext(True, True, True)) == RunwayModel.GEN4_5


@pytest.mark.asyncio
async def test_fake_task_is_stable_and_polling_resumes_without_submission():
    provider = FakeVideoProvider(polls_before_completion=2)
    first = await provider.submit(request(), "data:image/png;base64,x")
    with pytest.raises(PollingWindowExpired):
        await poll_task(provider, first.remote_task_id, max_polls=1, interval_seconds=0)
    result = await poll_task(provider, first.remote_task_id, max_polls=1, interval_seconds=0)
    assert result.status.value == "succeeded"
    assert provider.submissions == 1
