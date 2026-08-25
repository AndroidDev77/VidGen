from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from pytest import MonkeyPatch
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import Session

from services.subtitles.pipeline import SubtitlePipeline, SubtitlePipelineConfig
from vidgen.db.models import Project, SourceVideo
from vidgen.db.subtitle_models import SubtitleRun
from vidgen.db.transcription_models import Transcript
from vidgen.storage.asset_service import AssetService
from vidgen.storage.blob import FilesystemBlobStore

ROOT = Path(__file__).resolve().parents[1]


def test_subtitle_migration_up_down_up(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    database = tmp_path / "subtitle-migration.db"
    url = f"sqlite+pysqlite:///{database}"
    monkeypatch.setenv("VIDGEN_DATABASE_URL", url)
    config = Config(str(ROOT / "alembic.ini"))
    command.upgrade(config, "head")
    inspector = inspect(create_engine(url))
    assert {"subtitle_runs", "subtitle_candidates"} <= set(inspector.get_table_names())
    assert "subtitle_run_id" in {column["name"] for column in inspector.get_columns("transcripts")}
    command.downgrade(config, "0003_transcription")
    assert "subtitle_runs" not in inspect(create_engine(url)).get_table_names()
    command.upgrade(config, "head")
    command.check(config)


@pytest.mark.asyncio
async def test_subtitle_migration_downgrade_removes_subtitle_backed_data(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    golden_video: Path,
) -> None:
    database = tmp_path / "populated-subtitle-migration.db"
    url = f"sqlite+pysqlite:///{database}"
    monkeypatch.setenv("VIDGEN_DATABASE_URL", url)
    config = Config(str(ROOT / "alembic.ini"))
    command.upgrade(config, "head")
    engine = create_engine(url)
    store = FilesystemBlobStore(tmp_path / "blobs", b"secret")
    with Session(engine, expire_on_commit=False) as session:
        project = Project(name="subtitle migration", visual_style="flat")
        session.add(project)
        session.flush()
        assets = AssetService(session, store)
        source_asset = assets.store_file(
            path=golden_video,
            kind="source_video",
            media_type="video/mp4",
            project_id=project.id,
            idempotency_key="migration-source",
        )
        sidecar = assets.store(
            content=b"1\n00:00:00,000 --> 00:00:03,000\nSubtitle line\n",
            kind="subtitle",
            media_type="application/x-subrip",
            project_id=project.id,
            idempotency_key="migration-sidecar",
            metadata={"filename": "episode.en.srt"},
        )
        source = SourceVideo(
            project_id=project.id,
            asset_id=source_asset.id,
            filename="episode.mp4",
            duration_seconds=3,
            probe={},
        )
        session.add(source)
        session.commit()
        await SubtitlePipeline(
            session,
            store,
            config=SubtitlePipelineConfig(allow_provider_search=False),
        ).process(
            project_id=project.id,
            source_video_id=source.id,
            sidecar_asset_ids=(sidecar.id,),
            idempotency_key="migration-import",
        )
        assert session.scalar(select(SubtitleRun)) is not None
        assert session.scalar(select(Transcript)) is not None
    engine.dispose()

    command.downgrade(config, "0003_transcription")
    downgraded = create_engine(url)
    with downgraded.connect() as connection:
        assert connection.execute(select(Transcript.__table__.c.id)).first() is None
    assert "subtitle_runs" not in inspect(downgraded).get_table_names()
