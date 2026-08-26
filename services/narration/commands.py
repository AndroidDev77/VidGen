"""CLI/worker construction for T12."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from services.narration.elevenlabs_adapter import ElevenLabsNarrationProvider
from services.narration.fake_provider import FakeNarrationProvider
from services.narration.openai_adapter import OpenAINarrationProvider
from services.narration.pipeline import NarrationPipeline
from services.narration.providers import NarrationProvider
from vidgen.contracts.narration import NarrationResult
from vidgen.storage.blob import BlobStore


@dataclass(frozen=True, slots=True)
class NarrationCommandOptions:
    provider: str = "fake"
    voice_profile_id: UUID | None = None
    idempotency_key: str | None = None
    openai_api_key: str | None = None
    elevenlabs_api_key: str | None = None


def build_provider(options: NarrationCommandOptions) -> NarrationProvider:
    if options.provider == "fake":
        return FakeNarrationProvider()
    if options.provider == "openai":
        if not options.openai_api_key:
            raise ValueError("OpenAI API key is required")
        return OpenAINarrationProvider(options.openai_api_key)
    if options.provider == "elevenlabs":
        if not options.elevenlabs_api_key:
            raise ValueError("ElevenLabs API key is required")
        return ElevenLabsNarrationProvider(options.elevenlabs_api_key)
    raise ValueError(f"unsupported narration provider: {options.provider}")


async def generate_narration(
    session: Session,
    blob_store: BlobStore,
    *,
    project_id: UUID,
    options: NarrationCommandOptions,
    provider: NarrationProvider | None = None,
) -> NarrationResult:
    if options.voice_profile_id is None:
        raise ValueError("a voice profile ID is required")
    return await NarrationPipeline(
        session, blob_store, provider or build_provider(options)
    ).process(
        project_id=project_id,
        voice_profile_id=options.voice_profile_id,
        idempotency_key=options.idempotency_key or f"narration:{uuid4()}",
    )
