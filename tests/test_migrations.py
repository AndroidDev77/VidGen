from __future__ import annotations

from io import StringIO
from pathlib import Path

from alembic import command
from alembic.config import Config
from pytest import MonkeyPatch
from sqlalchemy import create_engine, inspect

ROOT = Path(__file__).resolve().parents[1]


def test_initial_migration_up_down_up(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    database = tmp_path / "migration.db"
    url = f"sqlite+pysqlite:///{database}"
    monkeypatch.setenv("VIDGEN_DATABASE_URL", url)
    config = Config(str(ROOT / "alembic.ini"))

    command.upgrade(config, "head")
    assert "projects" in inspect(create_engine(url)).get_table_names()

    command.downgrade(config, "base")
    assert "projects" not in inspect(create_engine(url)).get_table_names()

    command.upgrade(config, "head")
    assert "render_jobs" in inspect(create_engine(url)).get_table_names()


def test_migrations_render_in_offline_mode(monkeypatch: MonkeyPatch) -> None:
    url = "postgresql+psycopg://vidgen:vidgen@localhost:5432/vidgen_offline"
    monkeypatch.setenv("VIDGEN_DATABASE_URL", url)
    output = StringIO()
    config = Config(str(ROOT / "alembic.ini"), output_buffer=output)
    command.upgrade(config, "head", sql=True)
    rendered = output.getvalue()
    assert "CREATE TABLE projects" in rendered
    assert "CREATE TABLE upload_sessions" in rendered
