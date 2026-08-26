"""Shared entrypoints for running T11 script generation from the CLI or a worker."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from services.script.fake_provider import FakeScriptGenerationProvider
from services.script.openai_adapter import OpenAIScriptConfig, OpenAIScriptGenerationProvider
from services.script.pipeline import ScriptGenerationPipeline
from services.script.provider import ScriptGenerationProvider
from vidgen.contracts.script import ScriptGenerationResult
from vidgen.storage.blob import BlobStore


@dataclass(frozen=True, slots=True)
class ScriptCommandOptions:
    provider: str = "fake"
    idempotency_key: str | None = None
    target_duration_ms: int | None = None
    target_words: int | None = None
    humor_intensity: float | None = None
    recap_mode: str | None = None
    openai_api_key: str | None = None
    compressor_model: str = "gpt-5.6"
    writer_model: str = "gpt-5.6"
    editor_model: str = "gpt-5.6"


def build_provider(options: ScriptCommandOptions) -> ScriptGenerationProvider:
    if options.provider == "fake":
        return FakeScriptGenerationProvider()
    if options.provider == "openai":
        if not options.openai_api_key:
            raise ValueError("an OpenAI API key is required for --provider openai")
        return OpenAIScriptGenerationProvider(
            OpenAIScriptConfig(
                api_key=options.openai_api_key,
                compressor_model=options.compressor_model,
                writer_model=options.writer_model,
                editor_model=options.editor_model,
            )
        )
    raise ValueError(f"unsupported provider: {options.provider}")


async def generate_script(
    session: Session,
    blob_store: BlobStore,
    *,
    project_id: UUID,
    options: ScriptCommandOptions,
    provider: ScriptGenerationProvider | None = None,
) -> ScriptGenerationResult:
    resolved_provider = provider or build_provider(options)
    overrides = {
        "target_duration_ms": options.target_duration_ms,
        "target_words": options.target_words,
        "humor_intensity": options.humor_intensity,
        "recap_mode": options.recap_mode,
    }
    idempotency_key = options.idempotency_key or f"script-generation:{uuid4()}"
    return await ScriptGenerationPipeline(session, blob_store, resolved_provider).process(
        project_id=project_id, idempotency_key=idempotency_key, setting_overrides=overrides
    )
