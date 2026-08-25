from __future__ import annotations

from uuid import uuid4

import pytest

from services.subtitles.acquisition import TranscriptAcquisitionService
from services.subtitles.pipeline import SubtitleUnavailableError


class _UnavailableSubtitles:
    async def process(self, **kwargs: object) -> object:
        del kwargs
        raise SubtitleUnavailableError("none")


class _FakeTranscription:
    def __init__(self) -> None:
        self.arguments: dict[str, object] = {}

    async def process(self, **kwargs: object) -> str:
        self.arguments = kwargs
        return "fallback-result"


@pytest.mark.asyncio
async def test_audio_transcription_runs_only_when_subtitles_are_unavailable() -> None:
    transcription = _FakeTranscription()
    service = TranscriptAcquisitionService(  # type: ignore[arg-type]
        _UnavailableSubtitles(), transcription
    )
    project_id = uuid4()
    result = await service.process(
        project_id=project_id,
        source_video_id=uuid4(),
        source_audio_asset_id=uuid4(),
        idempotency_key="acquire",
    )
    assert result == "fallback-result"
    assert transcription.arguments["project_id"] == project_id
    assert transcription.arguments["idempotency_key"] == "acquire:audio-transcription"
