from __future__ import annotations

import argparse
import asyncio
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api.settings import get_settings
from services.transcription.fake import FakeTranscriptionProvider
from services.transcription.openai_adapter import OpenAITranscriptionAdapter
from services.transcription.pipeline import TranscriptionPipeline
from vidgen.db.models import Asset, AudioAsset, SourceVideo
from vidgen.db.session import build_engine
from vidgen.storage.blob import FilesystemBlobStore


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    engine = build_engine(settings.database_url)
    blob_store = FilesystemBlobStore(settings.blob_root, settings.signing_secret.encode())
    provider = (
        FakeTranscriptionProvider()
        if args.provider == "fake"
        else OpenAITranscriptionAdapter(
            api_key=settings.openai_api_key or "",
            transcription_model=settings.transcription_model,
            diarization_model=settings.diarization_model,
        )
    )
    try:
        with Session(engine, expire_on_commit=False) as session:
            source = session.scalar(
                select(SourceVideo)
                .where(SourceVideo.project_id == args.project_id)
                .order_by(SourceVideo.created_at.desc(), SourceVideo.id.desc())
            )
            if source is None:
                raise ValueError("project has no finalized source video")
            audio = session.scalar(
                select(AudioAsset)
                .where(
                    AudioAsset.project_id == args.project_id,
                    AudioAsset.kind == "transcription_audio",
                )
                .order_by(AudioAsset.created_at.desc(), AudioAsset.id.desc())
            )
            if audio is None or session.get(Asset, audio.asset_id) is None:
                raise ValueError("project has no normalized transcription audio; run T06 first")
            result = await TranscriptionPipeline(session, blob_store, provider).process(
                project_id=args.project_id,
                source_video_id=source.id,
                source_audio_asset_id=audio.asset_id,
                idempotency_key=args.idempotency_key or f"transcription:{audio.asset_id}:v1",
                language_hint=args.language,
            )
            print(result.model_dump_json(indent=2))
    finally:
        if isinstance(provider, OpenAITranscriptionAdapter):
            await provider.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Transcribe normalized VidGen audio")
    parser.add_argument("project_id", type=UUID)
    parser.add_argument("--provider", choices=("fake", "openai"), default="fake")
    parser.add_argument("--idempotency-key")
    parser.add_argument("--language")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
