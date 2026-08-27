"""Provider-neutral asynchronous video boundary and capability registry."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from vidgen.contracts.animation import VideoProviderRequest, VideoProviderTask


@dataclass(frozen=True, slots=True)
class VideoCapability:
    model: str
    durations: tuple[int, ...]
    dimensions: tuple[tuple[int, int], ...]
    prompt_characters: int = 1000
    image_to_video: bool = True
    supports_last_frame: bool = False
    formats: tuple[str, ...] = ("mp4",)
    max_input_bytes: int = 5 * 1024 * 1024


CAPABILITIES = {
    model: VideoCapability(model, tuple(range(2, 11)), ((1280, 720), (1584, 672), (1104, 832)))
    for model in ("gen4_turbo", "gen4.5")
}


def validate_request(request: VideoProviderRequest) -> None:
    capability = CAPABILITIES[request.model.value]
    if request.requested_duration_seconds not in capability.durations:
        raise ValueError(
            f"unsupported_duration: {request.requested_duration_seconds}; "
            "supported values are 2-10 whole seconds"
        )
    if (request.width, request.height) not in capability.dimensions:
        raise ValueError(f"unsupported_dimensions: {request.width}:{request.height}")
    if len(request.compiled_motion_prompt) > capability.prompt_characters:
        raise ValueError("invalid_motion_prompt: provider prompt limit exceeded")
    if request.last_keyframe_asset_id and not capability.supports_last_frame:
        raise ValueError(f"unsupported_strict_last_frame: {request.model.value}")


class VideoGenerationProvider(Protocol):
    name: str

    async def submit(
        self, request: VideoProviderRequest, prompt_image: str
    ) -> VideoProviderTask: ...
    async def retrieve(self, remote_task_id: str) -> VideoProviderTask: ...
    async def cancel(self, remote_task_id: str) -> bool: ...
