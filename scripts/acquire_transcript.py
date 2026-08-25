from __future__ import annotations

import argparse
import asyncio
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api.settings import APISettings, get_settings
from services.subtitles.acquisition import TranscriptAcquisitionService
from services.subtitles.opensubtitles import OpenSubtitlesAdapter
from services.subtitles.pipeline import SubtitlePipeline, SubtitlePipelineConfig
from services.subtitles.providers import FakeSubtitleProvider, SubtitleProvider
from services.transcription.fake import FakeTranscriptionProvider
from services.transcription.openai_adapter import OpenAITranscriptionAdapter
from services.transcription.pipeline import TranscriptionPipeline
from vidgen.db.models import Asset, AudioAsset, SourceVideo
from vidgen.db.session import build_engine
from vidgen.providers.base import TranscriptionProvider
from vidgen.storage.blob import FilesystemBlobStore


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    engine = build_engine(settings.database_url)
    blob_store = FilesystemBlobStore(settings.blob_root, settings.signing_secret.encode())
    subtitle_provider = _subtitle_provider(args.subtitle_provider, settings)
    transcription_provider = _transcription_provider(args.transcription_provider, settings)
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
            subtitles = SubtitlePipeline(
                session,
                blob_store,
                subtitle_provider,
                config=SubtitlePipelineConfig(
                    languages=settings.subtitle_languages,
                    synchronize_provider_subtitles=settings.subtitle_sync_enabled,
                    allow_provider_search=subtitle_provider is not None,
                ),
            )
            transcription = (
                TranscriptionPipeline(session, blob_store, transcription_provider)
                if transcription_provider is not None
                else None
            )
            result = await TranscriptAcquisitionService(subtitles, transcription).process(
                project_id=args.project_id,
                source_video_id=source.id,
                source_audio_asset_id=audio.asset_id,
                sidecar_asset_ids=tuple(args.sidecar_asset_id),
                idempotency_key=args.idempotency_key or f"transcript-acquisition:{source.id}:v1",
                query=args.query,
                imdb_id=args.imdb_id,
                language_hint=args.language,
            )
            print(result.model_dump_json(indent=2))
    finally:
        if isinstance(subtitle_provider, OpenSubtitlesAdapter):
            await subtitle_provider.close()
        if isinstance(transcription_provider, OpenAITranscriptionAdapter):
            await transcription_provider.close()
    return 0


def _subtitle_provider(name: str, settings: APISettings) -> SubtitleProvider | None:
    if name == "none":
        return None
    if name == "fake":
        return FakeSubtitleProvider()
    return OpenSubtitlesAdapter(
        api_key=settings.opensubtitles_api_key or "",
        username=settings.opensubtitles_username,
        password=settings.opensubtitles_password,
    )


def _transcription_provider(name: str, settings: APISettings) -> TranscriptionProvider | None:
    if name == "none":
        return None
    if name == "fake":
        return FakeTranscriptionProvider()
    return OpenAITranscriptionAdapter(
        api_key=settings.openai_api_key or "",
        transcription_model=settings.transcription_model,
        diarization_model=settings.diarization_model,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Acquire subtitles first, then fall back to audio transcription"
    )
    parser.add_argument("project_id", type=UUID)
    parser.add_argument(
        "--subtitle-provider", choices=("opensubtitles", "fake", "none"), default="opensubtitles"
    )
    parser.add_argument(
        "--transcription-provider", choices=("openai", "fake", "none"), default="openai"
    )
    parser.add_argument("--idempotency-key")
    parser.add_argument("--query")
    parser.add_argument("--imdb-id")
    parser.add_argument("--language")
    parser.add_argument(
        "--sidecar-asset-id",
        action="append",
        default=[],
        type=UUID,
        help="Project-scoped subtitle asset UUID; repeat for multiple candidates",
    )
    return parser


def main() -> int:
    return asyncio.run(run(build_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
