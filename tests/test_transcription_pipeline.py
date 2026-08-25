from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

import vidgen.db.models
import vidgen.db.transcription_models  # noqa: F401
from services.transcription.chunker import ChunkerConfig
from services.transcription.fake import FakeTranscriptionProvider
from services.transcription.pipeline import TranscriptionPipeline
from vidgen.contracts.transcription import TranscriptionWarning
from vidgen.db.base import Base
from vidgen.db.models import Asset, AudioAsset, Project, SourceVideo
from vidgen.db.transcription_models import Transcript, TranscriptionChunk, TranscriptionRun
from vidgen.storage.asset_service import AssetService
from vidgen.storage.blob import FilesystemBlobStore


def _foundation(
    tmp_path: Path, golden_transcription_audio: Path
) -> tuple[Session, FilesystemBlobStore, Project, SourceVideo, Asset]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(engine, expire_on_commit=False)
    project = Project(name="transcription", visual_style="flat")
    session.add(project)
    session.flush()
    store = FilesystemBlobStore(tmp_path / "blobs", b"secret")
    service = AssetService(session, store)
    source_asset = service.store(
        content=b"source-video",
        kind="source_video",
        media_type="video/mp4",
        project_id=project.id,
        idempotency_key="source-video",
    )
    source = SourceVideo(
        project_id=project.id,
        asset_id=source_asset.id,
        filename="source.mp4",
        duration_seconds=6,
        probe={},
    )
    session.add(source)
    audio_asset = service.store_file(
        path=golden_transcription_audio,
        kind="audio",
        media_type="audio/wav",
        project_id=project.id,
        parent_asset_ids=(source_asset.id,),
        idempotency_key="transcription-audio",
    )
    session.add(
        AudioAsset(
            project_id=project.id,
            asset_id=audio_asset.id,
            kind="transcription_audio",
            duration_seconds=6,
            provider="ffmpeg",
        )
    )
    session.commit()
    asset = session.get(Asset, audio_asset.id)
    assert asset is not None
    return session, store, project, source, asset


@pytest.mark.asyncio
async def test_pipeline_is_idempotent_and_persists_provenance(
    tmp_path: Path, golden_transcription_audio: Path
) -> None:
    session, store, project, source, audio = _foundation(tmp_path, golden_transcription_audio)
    provider = FakeTranscriptionProvider()
    pipeline = TranscriptionPipeline(
        session,
        store,
        provider,
        chunker_config=ChunkerConfig(hard_duration_seconds=20),
    )
    first = await pipeline.process(
        project_id=project.id,
        source_video_id=source.id,
        source_audio_asset_id=audio.id,
        idempotency_key="golden-transcription",
    )
    asset_count = session.scalar(select(func.count()).select_from(Asset))
    second = await pipeline.process(
        project_id=project.id,
        source_video_id=source.id,
        source_audio_asset_id=audio.id,
        idempotency_key="golden-transcription",
    )
    assert first == second
    assert provider.transcription_calls == [0]
    assert provider.diarization_calls == [0]
    assert session.scalar(select(func.count()).select_from(Asset)) == asset_count
    assert first.coverage.ratio == 1
    assert first.language == "en"
    assert project.status == "transcribed"
    transcript_asset = session.get(Asset, first.transcript_asset_id)
    assert transcript_asset is not None
    assert transcript_asset.parents[0].id == audio.id
    assert session.scalar(select(func.count()).select_from(TranscriptionRun)) == 1
    assert session.scalar(select(func.count()).select_from(Transcript)) == 1
    run = session.scalar(select(TranscriptionRun))
    assert run is not None and run.language == "en"


@pytest.mark.asyncio
async def test_failed_chunk_retry_reuses_successful_checkpoint(
    tmp_path: Path, golden_transcription_audio: Path
) -> None:
    session, store, project, source, audio = _foundation(tmp_path, golden_transcription_audio)
    provider = FakeTranscriptionProvider(fail_once_sequences={1})
    pipeline = TranscriptionPipeline(
        session,
        store,
        provider,
        chunker_config=ChunkerConfig(hard_duration_seconds=3, overlap_seconds=1),
    )
    arguments = {
        "project_id": project.id,
        "source_video_id": source.id,
        "source_audio_asset_id": audio.id,
        "idempotency_key": "recoverable-transcription",
        "language_hint": "en",
    }
    with pytest.raises(TimeoutError):
        await pipeline.process(**arguments)
    result = await pipeline.process(**arguments)
    assert result.status == "transcribed"
    assert provider.transcription_calls.count(0) == 1
    assert provider.transcription_calls.count(1) == 2
    rows = list(session.scalars(select(TranscriptionChunk).order_by(TranscriptionChunk.sequence)))
    assert all(row.status == "complete" for row in rows)
    assert rows[0].attempt_count == 1


@pytest.mark.asyncio
async def test_low_coverage_fails_and_preserves_chunks(
    tmp_path: Path, golden_transcription_audio: Path
) -> None:
    session, store, project, source, audio = _foundation(tmp_path, golden_transcription_audio)
    provider = FakeTranscriptionProvider()
    pipeline = TranscriptionPipeline(
        session,
        store,
        provider,
        chunker_config=ChunkerConfig(hard_duration_seconds=20),
        minimum_coverage=1.0,
    )
    # The fake covers the complete chunk, so force a gap by changing the provider result after
    # its deterministic call through a small test-only wrapper.
    original = provider.transcribe

    async def partial(request: object, audio_path: Path):  # type: ignore[no-untyped-def]
        result = await original(request, audio_path)  # type: ignore[arg-type]
        return result.model_copy(update={"words": result.words[:1]})

    provider.transcribe = partial  # type: ignore[method-assign]
    with pytest.raises(ValueError, match="covers less"):
        await pipeline.process(
            project_id=project.id,
            source_video_id=source.id,
            source_audio_asset_id=audio.id,
            idempotency_key="low-coverage",
        )
    run = session.scalar(select(TranscriptionRun).where(TranscriptionRun.project_id == project.id))
    assert run is not None and run.status == "transcription_failed"
    assert session.scalar(select(func.count()).select_from(TranscriptionChunk)) == 1


@pytest.mark.asyncio
async def test_pipeline_rejects_audio_from_another_source_video(
    tmp_path: Path, golden_transcription_audio: Path
) -> None:
    session, store, project, _, audio = _foundation(tmp_path, golden_transcription_audio)
    service = AssetService(session, store)
    other_source_asset = service.store(
        content=b"other-source-video",
        kind="source_video",
        media_type="video/mp4",
        project_id=project.id,
        idempotency_key="other-source-video",
    )
    other_source = SourceVideo(
        project_id=project.id,
        asset_id=other_source_asset.id,
        filename="other.mp4",
        duration_seconds=6,
        probe={},
    )
    session.add(other_source)
    session.commit()
    with pytest.raises(ValueError, match="does not descend"):
        await TranscriptionPipeline(session, store, FakeTranscriptionProvider()).process(
            project_id=project.id,
            source_video_id=other_source.id,
            source_audio_asset_id=audio.id,
            idempotency_key="wrong-lineage",
        )


@pytest.mark.asyncio
async def test_idempotency_key_is_bound_to_complete_run_configuration(
    tmp_path: Path, golden_transcription_audio: Path
) -> None:
    session, store, project, source, audio = _foundation(tmp_path, golden_transcription_audio)
    arguments = {
        "project_id": project.id,
        "source_video_id": source.id,
        "source_audio_asset_id": audio.id,
        "idempotency_key": "configuration-bound",
    }
    await TranscriptionPipeline(
        session,
        store,
        FakeTranscriptionProvider(),
        chunker_config=ChunkerConfig(hard_duration_seconds=20),
    ).process(**arguments)
    with pytest.raises(ValueError, match="different transcription inputs"):
        await TranscriptionPipeline(
            session,
            store,
            FakeTranscriptionProvider(),
            chunker_config=ChunkerConfig(hard_duration_seconds=3),
        ).process(**arguments)


@pytest.mark.asyncio
async def test_provider_warnings_are_propagated_to_canonical_transcript(
    tmp_path: Path, golden_transcription_audio: Path
) -> None:
    session, store, project, source, audio = _foundation(tmp_path, golden_transcription_audio)
    provider = FakeTranscriptionProvider()
    original = provider.transcribe

    async def warned(request: object, audio_path: Path):  # type: ignore[no-untyped-def]
        result = await original(request, audio_path)  # type: ignore[arg-type]
        return result.model_copy(
            update={
                "warnings": [
                    TranscriptionWarning(code="provider_warning", message="degraded output")
                ]
            }
        )

    provider.transcribe = warned  # type: ignore[method-assign]
    result = await TranscriptionPipeline(
        session,
        store,
        provider,
        chunker_config=ChunkerConfig(hard_duration_seconds=20),
    ).process(
        project_id=project.id,
        source_video_id=source.id,
        source_audio_asset_id=audio.id,
        idempotency_key="warning-propagation",
    )
    assert any(warning.code == "provider_warning" for warning in result.warnings)
    transcript = session.get(Transcript, result.transcript_id)
    assert transcript is not None
    assert transcript.warnings[0]["code"] == "provider_warning"
