from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from pytest import MonkeyPatch
from sqlalchemy import create_engine, inspect

ROOT = Path(__file__).resolve().parents[1]


def test_transcription_migration_up_down_up(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    database = tmp_path / "transcription-migration.db"
    url = f"sqlite+pysqlite:///{database}"
    monkeypatch.setenv("VIDGEN_DATABASE_URL", url)
    config = Config(str(ROOT / "alembic.ini"))
    command.upgrade(config, "head")
    tables = set(inspect(create_engine(url)).get_table_names())
    assert {
        "transcription_runs",
        "transcription_chunks",
        "transcripts",
        "transcript_segments",
        "speaker_turns",
    } <= tables
    command.downgrade(config, "0002_ingestion")
    assert "transcription_runs" not in inspect(create_engine(url)).get_table_names()
    command.upgrade(config, "head")
    assert "speaker_turns" in inspect(create_engine(url)).get_table_names()
    command.check(config)
