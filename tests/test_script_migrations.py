from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from pytest import MonkeyPatch
from sqlalchemy import create_engine, inspect

ROOT = Path(__file__).resolve().parents[1]


def test_script_generation_migration_up_down_up(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    database = tmp_path / "script-migration.db"
    url = f"sqlite+pysqlite:///{database}"
    monkeypatch.setenv("VIDGEN_DATABASE_URL", url)
    config = Config(str(ROOT / "alembic.ini"))

    command.upgrade(config, "head")
    tables = set(inspect(create_engine(url)).get_table_names())
    assert {
        "script_generation_runs",
        "compressed_plot_plans",
        "scripts",
        "script_segments",
        "script_reviews",
        "script_edits",
    } <= tables

    command.downgrade(config, "0006_episode_analysis")
    tables_after_downgrade = set(inspect(create_engine(url)).get_table_names())
    assert "script_generation_runs" not in tables_after_downgrade
    assert "compressed_plot_plans" not in tables_after_downgrade
    assert "script_reviews" not in tables_after_downgrade
    assert "script_edits" not in tables_after_downgrade
    # ``scripts``/``script_segments`` predate T11 and revert to their T01 shape.
    assert "scripts" in tables_after_downgrade
    assert "script_segments" in tables_after_downgrade

    command.upgrade(config, "head")
    inspector = inspect(create_engine(url))
    assert "script_edits" in inspector.get_table_names()
    script_columns = {column["name"] for column in inspector.get_columns("scripts")}
    assert {"editing_pass", "rejection_reason"} <= script_columns
    run_columns = {column["name"] for column in inspector.get_columns("script_generation_runs")}
    assert "max_editing_passes" in run_columns
    command.check(config)


def test_editing_pass_columns_downgrade_cleanly(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    url = f"sqlite+pysqlite:///{tmp_path / 'passes.db'}"
    monkeypatch.setenv("VIDGEN_DATABASE_URL", url)
    config = Config(str(ROOT / "alembic.ini"))
    command.upgrade(config, "head")
    command.downgrade(config, "0023_visual_qa_force_approval")
    inspector = inspect(create_engine(url))
    script_columns = {column["name"] for column in inspector.get_columns("scripts")}
    assert "editing_pass" not in script_columns
    assert "rejection_reason" not in script_columns
    run_columns = {column["name"] for column in inspector.get_columns("script_generation_runs")}
    assert "max_editing_passes" not in run_columns
    command.upgrade(config, "head")
    command.check(config)


def test_single_alembic_head_after_t23_and_t11_merge(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    url = f"sqlite+pysqlite:///{tmp_path / 'heads.db'}"
    monkeypatch.setenv("VIDGEN_DATABASE_URL", url)
    config = Config(str(ROOT / "alembic.ini"))
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(config)
    # The chain always has exactly one head; its name moves with each roadmap
    # task, so assert the invariant rather than the current name.
    assert len(script.get_heads()) == 1
