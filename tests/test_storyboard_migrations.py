"""T13 migration, constraint, and schema-drift checks."""

from __future__ import annotations

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

from tests.storyboard_fixtures import build_fixture
from tests.test_storyboard_pipeline import run_pipeline
from vidgen.db.storyboard_models import (
    StoryboardRepairAttempt,
    StoryboardRun,
    StoryboardSegmentCheckpoint,
    StoryboardShotRecord,
)

ROOT = Path(__file__).resolve().parents[1]
T13_TABLES = (
    "storyboard_runs",
    "storyboard_segment_checkpoints",
    "storyboard_shots",
    "storyboard_repair_attempts",
)


def _config() -> Config:
    return Config(str(ROOT / "alembic.ini"))


def test_exactly_one_alembic_head() -> None:
    heads = ScriptDirectory.from_config(_config()).get_heads()
    assert len(heads) == 1, f"expected a single Alembic head, found {heads}"


def test_storyboard_migration_follows_the_narration_head() -> None:
    script = ScriptDirectory.from_config(_config())
    revision = script.get_revision("0010_storyboard")
    assert revision.down_revision == "0009_narration"
    assert list(script.get_heads()) == ["0011_image_generation"]


def test_storyboard_migration_up_down_up(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    url = f"sqlite+pysqlite:///{tmp_path / 'storyboard-migration.db'}"
    monkeypatch.setenv("VIDGEN_DATABASE_URL", url)
    config = _config()
    engine = create_engine(url)

    command.upgrade(config, "head")
    assert set(T13_TABLES) <= set(inspect(engine).get_table_names())

    command.downgrade(config, "0009_narration")
    assert not set(T13_TABLES) & set(inspect(engine).get_table_names())

    command.upgrade(config, "head")
    assert set(T13_TABLES) <= set(inspect(engine).get_table_names())


def test_storyboard_migration_renders_offline_for_postgresql(monkeypatch: MonkeyPatch) -> None:
    from io import StringIO

    monkeypatch.setenv(
        "VIDGEN_DATABASE_URL", "postgresql+psycopg://vidgen:vidgen@localhost:5432/vidgen_offline"
    )
    output = StringIO()
    command.upgrade(Config(str(ROOT / "alembic.ini"), output_buffer=output), "head", sql=True)
    rendered = output.getvalue()
    for table in T13_TABLES:
        assert f"CREATE TABLE {table}" in rendered


def test_storyboard_downgrade_refuses_to_destroy_persisted_provenance(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    url = f"sqlite+pysqlite:///{tmp_path / 'storyboard-populated.db'}"
    monkeypatch.setenv("VIDGEN_DATABASE_URL", url)
    config = _config()
    command.upgrade(config, "head")
    engine = create_engine(url)
    with Session(engine) as session:
        from vidgen.db.models import Project

        project = Project(name="downgrade", visual_style="flat")
        session.add(project)
        session.flush()
        session.execute(
            StoryboardRun.__table__.insert().values(
                id=uuid4(),
                project_id=project.id,
                episode_model_id=uuid4(),
                script_id=uuid4(),
                script_version=1,
                narration_run_id=uuid4(),
                capability_profile_id="runway-gen4-turbo",
                capability_hash="a" * 64,
                idempotency_key="downgrade",
                input_hash="b" * 64,
                status="storyboard_complete",
                provider="fake",
                model="fake-storyboard-1",
                contract_version="storyboard/1.0",
                director_version="d",
                prompt_version="p",
                retimer_version="r",
                version=1,
                selected=False,
                segment_count=0,
                shot_count=0,
                total_duration_us=0,
                parameters={},
            )
        )
        session.commit()

    with pytest.raises(RuntimeError, match="unsafe T13 downgrade"):
        command.downgrade(config, "0009_narration")
    assert "storyboard_runs" in inspect(engine).get_table_names()


def test_no_alembic_schema_drift_for_storyboard_tables(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """The migration and the ORM models must describe the same tables."""
    url = f"sqlite+pysqlite:///{tmp_path / 'drift.db'}"
    monkeypatch.setenv("VIDGEN_DATABASE_URL", url)
    command.upgrade(_config(), "head")
    inspector = inspect(create_engine(url))
    for model in (
        StoryboardRun,
        StoryboardSegmentCheckpoint,
        StoryboardShotRecord,
        StoryboardRepairAttempt,
    ):
        table = model.__table__
        migrated = {column["name"] for column in inspector.get_columns(table.name)}
        assert {column.name for column in table.columns} == migrated


# -- database constraints -----------------------------------------------------


def test_run_idempotency_is_unique_per_project(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    result = run_pipeline(fixture)
    run = fixture.session.get(StoryboardRun, result.storyboard_run_id)
    fixture.session.add(
        StoryboardRun(
            project_id=run.project_id,
            episode_model_id=run.episode_model_id,
            script_id=run.script_id,
            script_version=run.script_version,
            narration_run_id=run.narration_run_id,
            capability_profile_id=run.capability_profile_id,
            capability_hash=run.capability_hash,
            idempotency_key=run.idempotency_key,
            input_hash=run.input_hash,
            status="storyboard_queued",
            provider="fake",
            model="fake-storyboard-1",
            contract_version=run.contract_version,
            director_version=run.director_version,
            prompt_version=run.prompt_version,
            retimer_version=run.retimer_version,
            version=99,
        )
    )
    with pytest.raises(IntegrityError):
        fixture.session.commit()


def test_shot_global_sequence_is_unique_within_a_run(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    result = run_pipeline(fixture)
    existing = fixture.session.query(StoryboardShotRecord).first()
    clone = StoryboardShotRecord(
        storyboard_run_id=existing.storyboard_run_id,
        segment_checkpoint_id=existing.segment_checkpoint_id,
        stable_shot_id=uuid4(),
        global_sequence=existing.global_sequence,
        segment_sequence=99,
        script_segment_id=existing.script_segment_id,
        narration_segment_id=existing.narration_segment_id,
        start_us=0,
        end_us=1_000_000,
        global_start_us=0,
        global_end_us=1_000_000,
        usable_duration_us=1_000_000,
        requested_generation_duration_us=1_000_000,
        trim_start_us=0,
        trim_end_us=0,
        transition_handle_us=0,
        word_start_index=0,
        word_end_index=1,
        camera={},
        action={},
        transition_in={},
        transition_out={},
        references={},
        incoming_continuity={},
        outgoing_continuity={},
        contract={},
    )
    fixture.session.add(clone)
    with pytest.raises(IntegrityError):
        fixture.session.commit()
    assert result.shot_count > 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("usable_duration_us", 0),
        ("end_us", 0),
        ("trim_start_us", -1),
        ("requested_generation_duration_us", 1),
    ],
)
def test_shot_check_constraints_reject_invalid_timing(
    tmp_path: Path, field: str, value: int
) -> None:
    fixture = build_fixture(tmp_path, database_name=f"constraint-{field}.db")
    run_pipeline(fixture)
    existing = fixture.session.query(StoryboardShotRecord).first()
    payload = {
        "storyboard_run_id": existing.storyboard_run_id,
        "segment_checkpoint_id": existing.segment_checkpoint_id,
        "stable_shot_id": uuid4(),
        "global_sequence": 900,
        "segment_sequence": 900,
        "script_segment_id": existing.script_segment_id,
        "narration_segment_id": existing.narration_segment_id,
        "start_us": 0,
        "end_us": 2_000_000,
        "global_start_us": 0,
        "global_end_us": 2_000_000,
        "usable_duration_us": 2_000_000,
        "requested_generation_duration_us": 2_000_000,
        "trim_start_us": 0,
        "trim_end_us": 0,
        "transition_handle_us": 0,
        "word_start_index": 0,
        "word_end_index": 1,
        "camera": {},
        "action": {},
        "transition_in": {},
        "transition_out": {},
        "references": {},
        "incoming_continuity": {},
        "outgoing_continuity": {},
        "contract": {},
    }
    payload[field] = value
    fixture.session.add(StoryboardShotRecord(**payload))
    with pytest.raises(IntegrityError):
        fixture.session.commit()


def test_repair_attempt_number_is_bounded(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    run_pipeline(fixture)
    checkpoint = fixture.session.query(StoryboardSegmentCheckpoint).first()
    fixture.session.add(
        StoryboardRepairAttempt(
            segment_checkpoint_id=checkpoint.id,
            attempt_number=9,
            idempotency_key="too-many",
            input_diagnostics=[],
            status="pending",
        )
    )
    with pytest.raises(IntegrityError):
        fixture.session.commit()
