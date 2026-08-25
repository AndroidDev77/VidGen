from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from pytest import MonkeyPatch
from sqlalchemy import create_engine, inspect


def test_initial_migration_up_down_up(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    database = tmp_path / "migration.db"
    url = f"sqlite+pysqlite:///{database}"
    monkeypatch.setenv("VIDGEN_DATABASE_URL", url)
    config = Config("alembic.ini")

    command.upgrade(config, "head")
    assert "projects" in inspect(create_engine(url)).get_table_names()

    command.downgrade(config, "base")
    assert "projects" not in inspect(create_engine(url)).get_table_names()

    command.upgrade(config, "head")
    assert "render_jobs" in inspect(create_engine(url)).get_table_names()
