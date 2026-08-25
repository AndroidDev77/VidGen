from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

import vidgen.db.models
import vidgen.db.subtitle_models
import vidgen.db.transcription_models  # noqa: F401
from services.subtitles.pipeline import SubtitlePipeline, SubtitlePipelineConfig
from services.subtitles.providers import FakeSubtitleProvider
from vidgen.contracts.subtitles import CanonicalSubtitleTranscriptArtifact
from vidgen.db.base import Base
from vidgen.db.models import Asset, Project, SourceVideo
from vidgen.db.subtitle_models import SubtitleCandidateRecord, SubtitleRun
from vidgen.db.transcription_models import Transcript
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
