from __future__ import annotations

from io import StringIO
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from pytest import MonkeyPatch
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session

from vidgen.db.models import Asset, Project

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


def test_ingestion_downgrade_refuses_to_destroy_deduplicated_provenance(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    database = tmp_path / "migration-with-assets.db"
    url = f"sqlite+pysqlite:///{database}"
    monkeypatch.setenv("VIDGEN_DATABASE_URL", url)
    config = Config(str(ROOT / "alembic.ini"))
    command.upgrade(config, "head")
    engine = create_engine(url)
    with Session(engine) as session:
        first_project = Project(name="first", visual_style="flat")
        second_project = Project(name="second", visual_style="flat")
        session.add_all([first_project, second_project])
        session.flush()
        session.add_all(
            [
                Asset(
                    project_id=project.id,
                    kind="source_video",
                    sha256="a" * 64,
                    byte_size=3_000_000_000,
                    media_type="video/mp4",
                    storage_key="sha256/aa/shared.mp4",
                    generation_parameters={},
                    extra_metadata={},
                )
                for project in (first_project, second_project)
            ]
        )
        session.commit()

    with pytest.raises(RuntimeError, match="duplicate asset blobs"):
        command.downgrade(config, "0001_core")

    assert "upload_sessions" in inspect(engine).get_table_names()
