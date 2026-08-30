"""T18b migration and schema-invariant tests.

The control-plane tables carry the invariant, not just the data. These tests
prove the constraints exist and bite, that the migration is reversible while the
tables are empty and refuses to destroy provenance once they are not, and that
the repository leaves exactly one Alembic head.
"""

from __future__ import annotations

from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from pytest import MonkeyPatch
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from vidgen.contracts.control_commands import ControlCommandStatus, ControlCommandType
from vidgen.db.control_command_models import ControlCommandRecord, ProjectGenerationRunRecord
from vidgen.db.models import Project

ROOT = Path(__file__).resolve().parents[1]
IDENTITY = "a" * 64


def _config(tmp_path: Path, monkeypatch: MonkeyPatch) -> tuple[Config, str]:
    url = f"sqlite+pysqlite:///{tmp_path / 'control-plane.db'}"
    monkeypatch.setenv("VIDGEN_DATABASE_URL", url)
    return Config(str(ROOT / "alembic.ini")), url


def _project(session: Session) -> Project:
    project = Project(name="control-plane", visual_style="flat", owner_subject="owner-a")
    session.add(project)
    session.flush()
    return project


def _command(project: Project, **overrides: object) -> ControlCommandRecord:
    values: dict[str, object] = {
        "project_id": project.id,
        "owner_subject": "owner-a",
        "command_type": ControlCommandType.FINAL_QA_RUN.value,
        "target_type": "project",
        "target_id": project.id,
        "idempotency_key": "k1",
        "request_hash": IDENTITY,
        "upstream_input_identity": IDENTITY,
        "status": ControlCommandStatus.PENDING.value,
    }
    values.update(overrides)
    return ControlCommandRecord(**values)  # type: ignore[arg-type]


def test_the_migration_creates_the_control_plane_tables(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    config, url = _config(tmp_path, monkeypatch)
    command.upgrade(config, "head")
    tables = set(inspect(create_engine(url)).get_table_names())
    assert {"control_commands", "project_generation_runs"} <= tables


def test_the_migration_is_reversible_while_the_tables_are_empty(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    config, url = _config(tmp_path, monkeypatch)
    command.upgrade(config, "head")
    command.downgrade(config, "0020_youtube_publication")
    tables = set(inspect(create_engine(url)).get_table_names())
    assert "control_commands" not in tables
    command.upgrade(config, "head")
    assert "control_commands" in set(inspect(create_engine(url)).get_table_names())


def test_the_downgrade_refuses_to_destroy_command_provenance(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """These rows are the only record of which workflow a command started."""
    config, url = _config(tmp_path, monkeypatch)
    command.upgrade(config, "head")
    engine = create_engine(url)
    with Session(engine) as session:
        project = _project(session)
        session.add(_command(project))
        session.commit()
    with pytest.raises(RuntimeError, match="control-plane provenance"):
        command.downgrade(config, "0020_youtube_publication")


def test_a_dispatched_command_must_name_its_workflow(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """The core T18b invariant, enforced by the database rather than by code."""
    config, url = _config(tmp_path, monkeypatch)
    command.upgrade(config, "head")
    engine = create_engine(url)
    for status in ("running", "awaiting_review", "completed"):
        with Session(engine) as session:
            project = _project(session)
            session.add(
                _command(project, status=status, idempotency_key=f"k-{status}", workflow_id=None)
            )
            with pytest.raises(IntegrityError):
                session.commit()


def test_a_claim_and_its_lease_are_stored_together(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """Half a claim is unrecoverable: the row would be held by nobody, forever."""
    config, url = _config(tmp_path, monkeypatch)
    command.upgrade(config, "head")
    engine = create_engine(url)
    with Session(engine) as session:
        project = _project(session)
        session.add(_command(project, claim_owner="dispatcher-a", lease_expires_at=None))
        with pytest.raises(IntegrityError):
            session.commit()


def test_a_command_is_unique_per_project_type_and_key(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    config, url = _config(tmp_path, monkeypatch)
    command.upgrade(config, "head")
    engine = create_engine(url)
    with Session(engine) as session:
        project = _project(session)
        session.add(_command(project))
        session.commit()
        session.add(_command(project))
        with pytest.raises(IntegrityError):
            session.commit()


def test_only_one_generation_run_can_be_active_per_project(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """Two concurrent revisions must not both claim the project's lineage."""
    config, url = _config(tmp_path, monkeypatch)
    command.upgrade(config, "head")
    engine = create_engine(url)
    with Session(engine) as session:
        project = _project(session)
        for sequence in (1, 2):
            session.add(
                ProjectGenerationRunRecord(
                    id=uuid4(),
                    project_id=project.id,
                    sequence=sequence,
                    status="active",
                    entry_stage="upload",
                    input_identity=IDENTITY,
                    active=True,
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
            )
        with pytest.raises(IntegrityError):
            session.commit()


def test_the_repository_has_exactly_one_alembic_head() -> None:
    heads = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini"))).get_heads()
    assert len(heads) == 1, f"expected one head, found {heads}"


def test_the_control_plane_migration_renders_offline_for_postgresql(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "VIDGEN_DATABASE_URL", "postgresql+psycopg://vidgen:vidgen@localhost:5432/vidgen_offline"
    )
    output = StringIO()
    command.upgrade(Config(str(ROOT / "alembic.ini"), output_buffer=output), "head", sql=True)
    rendered = output.getvalue()
    assert "CREATE TABLE control_commands" in rendered
    assert "CREATE TABLE project_generation_runs" in rendered
