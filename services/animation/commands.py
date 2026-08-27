"""Construction helpers for local and Temporal T15 execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from services.animation.fake_provider import FakeVideoProvider
from services.animation.pipeline import AnimationPipeline
from services.animation.providers import VideoGenerationProvider
from services.animation.runway import RunwayVideoProvider
from vidgen.contracts.animation import AnimationResult, RunwayModel
from vidgen.storage.blob import BlobStore


@dataclass(frozen=True, slots=True)
class AnimationCommandOptions:
    provider: str = "fake"
    model: RunwayModel | None = None
    width: int = 1280
    height: int = 720
    idempotency_key: str | None = None
    runway_api_key: str | None = None
    provider_configuration_version: str = "runway/2024-11-06"
    max_polls: int = 120
    poll_interval_seconds: float = 2


def build_provider(
    options: AnimationCommandOptions, client: Any | None = None
) -> VideoGenerationProvider:
    if options.provider == "fake":
        return FakeVideoProvider()
    if options.provider == "runway":
        if not options.runway_api_key:
            raise ValueError("RUNWAYML_API_SECRET is required")
        if client is None:
            from runwayml import AsyncRunwayML

            client = AsyncRunwayML(api_key=options.runway_api_key, max_retries=0)
        return RunwayVideoProvider(client)
    raise ValueError(f"unsupported video provider: {options.provider}")


async def generate_shot_videos(
    session: Session,
    blob_store: BlobStore,
    *,
    project_id: UUID,
    options: AnimationCommandOptions,
    provider: VideoGenerationProvider | None = None,
    storyboard_id: UUID | None = None,
    image_run_id: UUID | None = None,
    shot_id: UUID | None = None,
) -> AnimationResult:
    pipeline = AnimationPipeline(
        session,
        blob_store,
        provider or build_provider(options),
        requested_model=options.model,
        width=options.width,
        height=options.height,
        provider_configuration_version=options.provider_configuration_version,
        max_polls=options.max_polls,
        poll_interval_seconds=options.poll_interval_seconds,
    )
    return await pipeline.process(
        project_id=project_id,
        storyboard_id=storyboard_id,
        image_run_id=image_run_id,
        shot_id=shot_id,
        idempotency_key=options.idempotency_key or f"animation:{uuid4()}",
    )
