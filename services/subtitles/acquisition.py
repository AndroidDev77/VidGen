from __future__ import annotations

from uuid import UUID

from services.subtitles.pipeline import SubtitlePipeline, SubtitleUnavailableError
from services.transcription.pipeline import TranscriptionPipeline
from vidgen.contracts.subtitles import SubtitleImportResult
from vidgen.contracts.transcription import TranscriptionResult


class TranscriptAcquisitionService:
    """Prefer subtitles and invoke T07 audio transcription only when none are adequate."""

    def __init__(
        self,
        subtitles: SubtitlePipeline,
        transcription: TranscriptionPipeline | None = None,
    ) -> None:
        self.subtitles = subtitles
        self.transcription = transcription

    async def process(
        self,
        *,
        project_id: UUID,
        source_video_id: UUID,
        source_audio_asset_id: UUID,
        idempotency_key: str,
        sidecar_asset_ids: tuple[UUID, ...] = (),
        query: str | None = None,
        imdb_id: str | None = None,
        language_hint: str | None = None,
    ) -> SubtitleImportResult | TranscriptionResult:
        try:
            return await self.subtitles.process(
                project_id=project_id,
                source_video_id=source_video_id,
                source_audio_asset_id=source_audio_asset_id,
                sidecar_asset_ids=sidecar_asset_ids,
                query=query,
                imdb_id=imdb_id,
                idempotency_key=f"{idempotency_key}:subtitles",
            )
        except SubtitleUnavailableError:
            if self.transcription is None:
                raise
            return await self.transcription.process(
                project_id=project_id,
                source_video_id=source_video_id,
                source_audio_asset_id=source_audio_asset_id,
                language_hint=language_hint,
                idempotency_key=f"{idempotency_key}:audio-transcription",
            )
