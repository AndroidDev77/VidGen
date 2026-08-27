"""Construction helpers for local and Temporal T14 execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from services.image_generation.fake_provider import DeterministicFakeImageProvider
from services.image_generation.openai_image import OpenAIImageProvider
from services.image_generation.pipeline import ImageGenerationPipeline
from services.image_generation.providers import GPT_IMAGE_SNAPSHOT, ImageGenerationProvider
from vidgen.contracts.image_generation import ImageGenerationResult, KeyframeRole
from vidgen.storage.blob import BlobStore


@dataclass(frozen=True, slots=True)
class ImageGenerationCommandOptions:
    provider: str = "fake"
    model: str = GPT_IMAGE_SNAPSHOT
    width: int = 1536
    height: int = 864
    quality: str = "medium"
    idempotency_key: str | None = None
    openai_api_key: str | None = None
    provider_configuration_version: str = "image-provider/1"


def build_provider(
    options: ImageGenerationCommandOptions, client: Any | None = None
) -> ImageGenerationProvider:
    if options.provider == "fake":
        return DeterministicFakeImageProvider()
    if options.provider == "openai":
        if not options.openai_api_key:
            raise ValueError("VIDGEN_OPENAI_API_KEY is required")
        if client is None:
            from openai import OpenAI

            client = OpenAI(api_key=options.openai_api_key)
        return OpenAIImageProvider(client)
    raise ValueError(f"unsupported image provider: {options.provider}")


async def generate_keyframes(
    session: Session,
    blob_store: BlobStore,
    *,
    project_id: UUID,
    options: ImageGenerationCommandOptions,
    provider: ImageGenerationProvider | None = None,
    storyboard_id: UUID | None = None,
    shot_id: UUID | None = None,
    role: KeyframeRole | None = None,
) -> ImageGenerationResult:
    pipeline = ImageGenerationPipeline(
        session,
        blob_store,
        provider or build_provider(options),
        model=options.model,
        width=options.width,
        height=options.height,
        quality=options.quality,
        provider_configuration_version=options.provider_configuration_version,
    )
    return await pipeline.process(
        project_id=project_id,
        storyboard_id=storyboard_id,
        shot_id=shot_id,
        role=role,
        idempotency_key=options.idempotency_key or f"image-generation:{uuid4()}",
    )
