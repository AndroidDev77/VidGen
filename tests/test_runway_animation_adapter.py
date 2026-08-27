from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from services.animation.runway import RunwayVideoProvider
from vidgen.contracts.animation import RunwayModel, VideoProvider, VideoProviderRequest


class Resource:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response

    async def retrieve(self, value):
        self.calls.append(value)
        return self.response

    async def delete(self, value):
        self.calls.append(value)


def request() -> VideoProviderRequest:
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
        model=RunwayModel.GEN4_TURBO,
        requested_duration_seconds=4,
        width=1280,
        height=720,
        attempt_number=1,
        provider_configuration_version="runway/2024-11-06",
    )


def test_submission_maps_exact_request_and_retrieval_redacts_outputs() -> None:
    created = Resource(SimpleNamespace(id="task-1"))
    retrieved = Resource(
        SimpleNamespace(
            status="SUCCEEDED",
            model="gen4_turbo",
            duration=4,
            created_at=datetime.now(UTC),
            output=["https://signed.invalid/output.mp4"],
            progress=1,
        )
    )
    client = SimpleNamespace(image_to_video=created, tasks=retrieved)
    provider = RunwayVideoProvider(client)
    submitted = asyncio.run(provider.submit(request(), "data:image/png;base64,abc"))
    assert submitted.remote_task_id == "task-1"
    assert created.calls[0] == {
        "model": "gen4_turbo",
        "prompt_image": "data:image/png;base64,abc",
        "prompt_text": "subject turns once",
        "duration": 4,
        "ratio": "1280:720",
    }
    result = asyncio.run(provider.retrieve("task-1"))
    assert result.status.value == "succeeded"
    assert result.output_handles == ("https://signed.invalid/output.mp4",)
    assert "signed.invalid" not in result.model_dump_json()
