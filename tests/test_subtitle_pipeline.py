from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

import vidgen.db.models
import vidgen.db.subtitle_models
import vidgen.db.transcription_models  # noqa: F401
from packages.providers import FakeSubtitleProvider
from services.subtitles.pipeline import SubtitlePipeline, SubtitlePipelineConfig
from vidgen.contracts.subtitles import CanonicalSubtitleTranscriptArtifact
from vidgen.db.base import Base
from vidgen.db.models import Asset, Project, SourceVideo
from vidgen.db.subtitle_models import SubtitleCandidateRecord, SubtitleRun
from vidgen.db.transcription_models import Transcript, TranscriptionRun
from vidgen.db.transcription_repository import TranscriptionRepository
from vidgen.storage.asset_service import AssetService
from vidgen.storage.blob import FilesystemBlobStore


def _foundation(
    tmp_path: Path, golden_video: Path
) -> tuple[Session, FilesystemBlobStore, Project, SourceVideo, AssetService]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(engine, expire_on_commit=False)
    project = Project(name="subtitle", visual_style="flat")
    session.add(project)
    session.flush()
    store = FilesystemBlobStore(tmp_path / "blobs", b"secret")
    assets = AssetService(session, store)
    source_asset = assets.store_file(
        path=golden_video,
        kind="source_video",
        media_type="video/mp4",
        project_id=project.id,
        idempotency_key="source-video",
    )
    source = SourceVideo(
        project_id=project.id,
        asset_id=source_asset.id,
        filename="Example.Show.S01E02.2026.mp4",
        duration_seconds=3,
        probe={},
    )
    session.add(source)
    session.commit()
    return session, store, project, source, assets


@pytest.mark.asyncio
async def test_sidecar_is_imported_into_canonical_transcript_and_is_idempotent(
    tmp_path: Path, golden_video: Path
) -> None:
    session, store, project, source, assets = _foundation(tmp_path, golden_video)
    sidecar = assets.store(
        content=(
            b"1\n00:00:00,000 --> 00:00:01,200\nALICE: Hello there\n\n"
            b"2\n00:00:01,300 --> 00:00:02,800\nThe story continues\n"
        ),
        kind="subtitle",
        media_type="application/x-subrip",
        project_id=project.id,
        idempotency_key="sidecar-upload",
        metadata={"filename": "episode.en.srt"},
    )
    session.commit()
    pipeline = SubtitlePipeline(
        session,
        store,
        config=SubtitlePipelineConfig(allow_provider_search=False),
    )
    arguments = {
        "project_id": project.id,
        "source_video_id": source.id,
        "idempotency_key": "subtitle-import",
        "sidecar_asset_ids": (sidecar.id,),
    }
    first = await pipeline.process(**arguments)
    asset_count = session.scalar(select(func.count()).select_from(Asset))
    second = await pipeline.process(**arguments)
    assert first == second
    assert session.scalar(select(func.count()).select_from(Asset)) == asset_count
    assert first.text == "Hello there The story continues"
    assert first.candidate.source_type == "sidecar"
    assert project.status == "transcribed"
    canonical_asset = session.get(Asset, first.transcript_asset_id)
    assert canonical_asset is not None
    CanonicalSubtitleTranscriptArtifact.model_validate_json(store.read(canonical_asset.storage_key))
    segment = session.scalar(select(Transcript).where(Transcript.id == first.transcript_id))
    assert segment is not None and segment.run_id is None
    assert segment.subtitle_run_id == first.subtitle_run_id


@pytest.mark.asyncio
async def test_provider_search_uses_filename_identity_and_persists_provenance(
    tmp_path: Path, golden_video: Path
) -> None:
    session, store, project, source, _ = _foundation(tmp_path, golden_video)
    provider = FakeSubtitleProvider(
        b"1\n00:00:00,000 --> 00:00:01,500\nProvider line\n\n"
        b"2\n00:00:01,500 --> 00:00:03,000\nSecond line\n"
    )
    result = await SubtitlePipeline(session, store, provider).process(
        project_id=project.id,
        source_video_id=source.id,
        idempotency_key="provider-import",
    )
    assert result.candidate.provider == "fake-subtitles"
    assert provider.search_calls[0].query == "Example Show"
    assert provider.search_calls[0].season_number == 1
    assert provider.search_calls[0].episode_number == 2
    row = session.scalar(select(SubtitleCandidateRecord).where(SubtitleCandidateRecord.selected))
    assert row is not None and row.asset_id == result.source_subtitle_asset_id
    asset = session.get(Asset, row.asset_id)
    assert asset is not None and asset.parents[0].id == source.asset_id
    assert session.scalar(select(func.count()).select_from(SubtitleRun)) == 1


@pytest.mark.asyncio
async def test_local_sidecar_prevents_provider_request(tmp_path: Path, golden_video: Path) -> None:
    session, store, project, source, assets = _foundation(tmp_path, golden_video)
    sidecar = assets.store(
        content=b"1\n00:00:00,000 --> 00:00:03,000\nLocal subtitle\n",
        kind="subtitle",
        media_type="application/x-subrip",
        project_id=project.id,
        idempotency_key="local-sidecar",
        metadata={"filename": "episode.en.srt"},
    )
    session.commit()
    provider = FakeSubtitleProvider()
    result = await SubtitlePipeline(session, store, provider).process(
        project_id=project.id,
        source_video_id=source.id,
        sidecar_asset_ids=(sidecar.id,),
        idempotency_key="local-first",
    )
    assert result.candidate.source_type == "sidecar"
    assert provider.search_calls == []


@pytest.mark.asyncio
async def test_invalid_local_sidecar_falls_through_to_provider(
    tmp_path: Path, golden_video: Path
) -> None:
    session, store, project, source, assets = _foundation(tmp_path, golden_video)
    sidecar = assets.store(
        content=b"this is not a timed subtitle",
        kind="subtitle",
        media_type="application/x-subrip",
        project_id=project.id,
        idempotency_key="invalid-sidecar",
        metadata={"filename": "episode.en.srt"},
    )
    session.commit()
    provider = FakeSubtitleProvider(
        b"1\n00:00:00,000 --> 00:00:01,500\nProvider line\n\n"
        b"2\n00:00:01,500 --> 00:00:03,000\nSecond line\n"
    )
    result = await SubtitlePipeline(session, store, provider).process(
        project_id=project.id,
        source_video_id=source.id,
        sidecar_asset_ids=(sidecar.id,),
        idempotency_key="invalid-local-fallback",
    )
    assert result.candidate.source_type == "provider"
    assert len(provider.search_calls) == 1


@pytest.mark.asyncio
async def test_interrupted_provider_import_reuses_checkpoint_without_stale_selection(
    tmp_path: Path,
    golden_video: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, store, project, source, _ = _foundation(tmp_path, golden_video)
    provider = FakeSubtitleProvider(
        b"1\n00:00:00,000 --> 00:00:01,500\nProvider line\n\n"
        b"2\n00:00:01,500 --> 00:00:03,000\nSecond line\n"
    )
    original = SubtitlePipeline._persist_transcript
    interrupted = True

    def fail_once(self: SubtitlePipeline, *args: object, **kwargs: object) -> object:
        nonlocal interrupted
        if interrupted:
            interrupted = False
            raise RuntimeError("simulated interruption")
        return original(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(SubtitlePipeline, "_persist_transcript", fail_once)
    pipeline = SubtitlePipeline(session, store, provider)
    arguments = {
        "project_id": project.id,
        "source_video_id": source.id,
        "idempotency_key": "provider-resume",
    }
    with pytest.raises(RuntimeError, match="simulated interruption"):
        await pipeline.process(**arguments)
    run = session.scalar(select(SubtitleRun))
    row = session.scalar(select(SubtitleCandidateRecord))
    assert run is not None and not run.selected and run.selected_candidate_id is None
    assert row is not None and not row.selected and row.asset_id is not None
    assert len(provider.search_calls) == 1

    result = await pipeline.process(**arguments)
    assert result.status == "subtitle_imported"
    assert len(provider.search_calls) == 1
    assert len(provider.download_calls) == 1


@pytest.mark.asyncio
async def test_audio_transcript_selection_clears_selected_subtitle_run(
    tmp_path: Path, golden_video: Path
) -> None:
    session, store, project, source, assets = _foundation(tmp_path, golden_video)
    sidecar = assets.store(
        content=b"1\n00:00:00,000 --> 00:00:03,000\nSubtitle line\n",
        kind="subtitle",
        media_type="application/x-subrip",
        project_id=project.id,
        idempotency_key="selected-sidecar",
        metadata={"filename": "episode.en.srt"},
    )
    session.commit()
    await SubtitlePipeline(
        session,
        store,
        config=SubtitlePipelineConfig(allow_provider_search=False),
    ).process(
        project_id=project.id,
        source_video_id=source.id,
        sidecar_asset_ids=(sidecar.id,),
        idempotency_key="selected-subtitle",
    )
    subtitle_run = session.scalar(select(SubtitleRun))
    assert subtitle_run is not None and subtitle_run.selected

    audio_run = TranscriptionRun(
        project_id=project.id,
        source_video_id=source.id,
        source_audio_asset_id=source.asset_id,
        idempotency_key="later-audio",
        status="transcribed",
        language="en",
        chunker_version="test",
        provider="fake",
        transcription_model="fake",
        diarization_model="fake",
        parameters={},
        coverage_score=1,
    )
    session.add(audio_run)
    session.flush()
    transcript_asset = assets.store(
        content=b"{}",
        kind="json",
        media_type="application/json",
        project_id=project.id,
        idempotency_key="later-audio-transcript",
    )
    audio_transcript = Transcript(
        project_id=project.id,
        run_id=audio_run.id,
        subtitle_run_id=None,
        version=2,
        language="en",
        text="Audio transcript",
        transcript_asset_id=transcript_asset.id,
        duration_seconds=3,
        coverage_score=1,
    )
    session.add(audio_transcript)
    TranscriptionRepository(session).select_run_and_transcript(audio_run, audio_transcript)
    session.commit()
    session.refresh(subtitle_run)
    assert not subtitle_run.selected
