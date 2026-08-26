"""CLI and worker construction for T13."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from services.storyboard.fake_provider import FakeStoryboardDirector
from services.storyboard.openai_adapter import (
    OpenAIStoryboardConfig,
    OpenAIStoryboardDirector,
)
from services.storyboard.pipeline import StoryboardPipeline
from services.storyboard.providers import (
    DEFAULT_STORYBOARD_MODEL,
    StoryboardDirector,
    load_capability_profile,
)
from vidgen.contracts.storyboard import StoryboardResult, VisualProviderCapability
from vidgen.storage.blob import BlobStore


@dataclass(frozen=True, slots=True)
class StoryboardCommandOptions:
    provider: str = "fake"
    model: str = DEFAULT_STORYBOARD_MODEL
    capability_profile_id: str | None = None
    idempotency_key: str | None = None
    openai_api_key: str | None = None


def build_director(options: StoryboardCommandOptions) -> StoryboardDirector:
    if options.provider == "fake":
        return FakeStoryboardDirector()
    if options.provider == "openai":
        if not options.openai_api_key:
            raise ValueError("OpenAI API key is required for the production Storyboard Director")
        return OpenAIStoryboardDirector(
            OpenAIStoryboardConfig(api_key=options.openai_api_key, model=options.model)
        )
    raise ValueError(f"unsupported storyboard provider: {options.provider}")


def resolve_capability(options: StoryboardCommandOptions) -> VisualProviderCapability:
    return load_capability_profile(options.capability_profile_id)


async def generate_storyboard(
    session: Session,
    blob_store: BlobStore,
    *,
    project_id: UUID,
    options: StoryboardCommandOptions,
    director: StoryboardDirector | None = None,
) -> StoryboardResult:
    resolved = director or build_director(options)
    pipeline = StoryboardPipeline(
        session,
        blob_store,
        resolved,
        capability_profile_id=options.capability_profile_id,
    )
    return await pipeline.process(
        project_id=project_id,
        idempotency_key=options.idempotency_key or f"storyboard:{uuid4()}",
    )
