from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from pytest import MonkeyPatch
from sqlalchemy import create_engine, inspect

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
