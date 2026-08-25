from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import vidgen.db.models
import vidgen.db.transcription_models  # noqa: F401
from services.transcription.chunker import ChunkerConfig, create_audio_chunks
from vidgen.db.base import Base
from vidgen.db.models import Asset, Project
from vidgen.storage.asset_service import AssetService
from vidgen.storage.blob import FilesystemBlobStore


def test_chunks_are_silence_aware_bounded_and_stable(
    tmp_path: Path, golden_transcription_audio: Path
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(engine, expire_on_commit=False)
    project = Project(name="chunking", visual_style="flat")
    session.add(project)
    session.flush()
    store = FilesystemBlobStore(tmp_path / "blobs", b"secret")
    parent = AssetService(session, store).store_file(
        path=golden_transcription_audio,
        kind="audio",
        media_type="audio/wav",
        project_id=project.id,
        idempotency_key="source-audio",
    )
    session.commit()
    config = ChunkerConfig(max_bytes=80_000, hard_duration_seconds=3, overlap_seconds=1.5)
    first, voiced, duration = create_audio_chunks(
        source=golden_transcription_audio,
        workspace=tmp_path / "first",
        project_id=project.id,
        parent_audio_asset_id=parent.id,
        parent_sha256=parent.sha256,
        asset_service=AssetService(session, store),
        config=config,
    )
    session.commit()
    second, _, _ = create_audio_chunks(
        source=golden_transcription_audio,
        workspace=tmp_path / "second",
        project_id=project.id,
        parent_audio_asset_id=parent.id,
        parent_sha256=parent.sha256,
        asset_service=AssetService(session, store),
        config=config,
    )
    assert duration == 6
    assert voiced
    assert len(first) >= 2
    assert all(chunk.byte_size <= config.max_bytes for chunk in first)
    assert first[1].overlap_before_seconds == 1.5
    assert [(item.asset_id, item.sha256) for item in first] == [
        (item.asset_id, item.sha256) for item in second
    ]
    for chunk in first:
        asset = session.get(Asset, chunk.asset_id)
        assert asset is not None
        assert asset.parents[0].id == parent.id


def test_recursive_split_enforces_actual_encoded_size(
    tmp_path: Path, golden_transcription_audio: Path
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(engine, expire_on_commit=False)
    project = Project(id=uuid4(), name="recursive", visual_style="flat")
    session.add(project)
    store = FilesystemBlobStore(tmp_path / "blobs", b"secret")
    parent = AssetService(session, store).store_file(
        path=golden_transcription_audio,
        kind="audio",
        media_type="audio/wav",
        project_id=project.id,
        idempotency_key="recursive-source",
    )
    chunks, _, _ = create_audio_chunks(
        source=golden_transcription_audio,
        workspace=tmp_path / "chunks",
        project_id=project.id,
        parent_audio_asset_id=parent.id,
        parent_sha256=parent.sha256,
        asset_service=AssetService(session, store),
        config=ChunkerConfig(max_bytes=20_000, hard_duration_seconds=60, overlap_seconds=0),
    )
    assert len(chunks) > 1
    assert max(chunk.byte_size for chunk in chunks) <= 20_000
