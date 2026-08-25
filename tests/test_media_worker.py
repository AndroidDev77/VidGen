from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

import vidgen.db.models
import vidgen.db.upload_models  # noqa: F401
from services.media_worker.pipeline import MediaPipeline
from services.media_worker.probe import probe_media
from vidgen.db.base import Base
from vidgen.db.models import Asset, Project, Scene, SourceVideo
from vidgen.storage.asset_service import AssetService
from vidgen.storage.blob import FilesystemBlobStore


def test_probe_golden_media(golden_video: Path) -> None:
    result = probe_media(golden_video)
    assert result.duration_seconds == pytest.approx(3.0, abs=0.1)
    assert result.video.width == 320
    assert result.video.height == 180
    assert result.video.frame_rate == pytest.approx(30, abs=0.01)
    assert len(result.audio_streams) == 1


def test_media_pipeline_is_deterministic_and_preserves_provenance(
    tmp_path: Path, golden_video: Path
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(engine, expire_on_commit=False)
    project = Project(name="golden", visual_style="flat")
    session.add(project)
    session.flush()
    store = FilesystemBlobStore(tmp_path / "blobs", b"secret")
    source_asset = AssetService(session, store).store_file(
        path=golden_video,
        kind="source_video",
        media_type="video/mp4",
        project_id=project.id,
        idempotency_key="golden-source",
    )
    source = SourceVideo(
        project_id=project.id,
        asset_id=source_asset.id,
        filename="golden.mp4",
        probe={},
    )
    session.add(source)
    session.commit()

    pipeline = MediaPipeline(session, store)
    first = pipeline.process(
        project_id=project.id,
        source_video_id=source.id,
        idempotency_key="golden-media",
    )
    asset_count = session.scalar(select(func.count()).select_from(Asset))
    second = pipeline.process(
        project_id=project.id,
        source_video_id=source.id,
        idempotency_key="golden-media",
    )
    assert first == second
    assert session.scalar(select(func.count()).select_from(Asset)) == asset_count
    assert len(first.scene_detection.scenes) == 3
    assert [scene.start_seconds for scene in first.scene_detection.scenes] == pytest.approx(
        [0, 1, 2], abs=0.15
    )
    assert len(first.frames) == 3
    assert len({frame.sha256 for frame in first.frames}) == 3
    assert first.audio.sample_rate == 16000
    assert first.audio.channels == 1
    assert project.status == "media_ready"

    frame_assets = list(
        session.scalars(
            select(Asset).where(Asset.id.in_([frame.asset_id for frame in first.frames]))
        )
    )
    assert all(asset.parents[0].id == source_asset.id for asset in frame_assets)
    scenes = list(session.scalars(select(Scene).where(Scene.project_id == project.id)))
    assert len(scenes) == 3
