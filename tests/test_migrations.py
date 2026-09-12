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
    assert "CREATE TABLE transcription_runs" in rendered


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


def test_every_revision_id_fits_the_alembic_version_column() -> None:
    """A long revision id breaks PostgreSQL and nothing else notices.

    ``alembic_version.version_num`` is ``VARCHAR(32)`` unless ``env.py`` widens
    it, and this project does not. SQLite ignores the declared length, so the
    whole test suite passes while ``alembic upgrade head`` aborts on the real
    database with "value too long". The bound belongs in a test, not in a
    reviewer's memory.
    """
    from alembic.script import ScriptDirectory

    scripts = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    too_long = {
        revision.revision: len(revision.revision)
        for revision in scripts.walk_revisions()
        if len(revision.revision) > 32
    }
    assert not too_long, f"revision ids exceed alembic_version.version_num: {too_long}"


def test_an_offline_script_never_adds_a_column_it_already_created(
    monkeypatch: MonkeyPatch,
) -> None:
    """A rendered ``--sql`` upgrade from base has to be applicable as written.

    Some tables are created from live model metadata rather than from frozen
    DDL, so a later additive migration renders an ``ADD COLUMN`` for a column
    the same script already declared in its ``CREATE TABLE``. Applying that
    fails on "column already exists", and no online test can see it. Such a
    statement must be written to tolerate the column being there.
    """
    url = "postgresql+psycopg://vidgen:vidgen@localhost:5432/vidgen_offline"
    monkeypatch.setenv("VIDGEN_DATABASE_URL", url)
    output = StringIO()
    command.upgrade(Config(str(ROOT / "alembic.ini"), output_buffer=output), "head", sql=True)

    created: dict[str, set[str]] = {}
    for raw in output.getvalue().split(";"):
        # Alembic interleaves ``-- Running upgrade`` comments with the DDL; a
        # statement still carrying one does not start with its own verb.
        statement = " ".join(
            word
            for line in raw.splitlines()
            if not line.strip().startswith("--")
            for word in line.split()
        )
        upper = statement.upper()
        if upper.startswith("CREATE TABLE"):
            table = statement.split()[2].strip("(").lower()
            body = statement[statement.index("(") + 1 :]
            created[table] = {
                part.strip().split()[0].lower()
                for part in body.split(",")
                if part.strip() and not part.strip().upper().startswith(_CONSTRAINT_WORDS)
            }
        elif upper.startswith("ALTER TABLE") and " ADD COLUMN " in f"{upper} ":
            words = statement.split()
            table = words[2].lower()
            column = words[words.index("COLUMN") + 1].lower()
            if column == "if":  # ADD COLUMN IF NOT EXISTS - safe by construction
                continue
            assert column not in created.get(table, set()), (
                f"{table}.{column} is added by a statement that already created it: {statement}"
            )


#: Leading words of a ``CREATE TABLE`` body element that names a constraint
#: rather than a column.
_CONSTRAINT_WORDS = ("PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "CONSTRAINT")
