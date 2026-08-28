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
    assert "script_edits" in inspect(create_engine(url)).get_table_names()
    command.check(config)


def test_single_alembic_head_after_t23_and_t11_merge(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    url = f"sqlite+pysqlite:///{tmp_path / 'heads.db'}"
    monkeypatch.setenv("VIDGEN_DATABASE_URL", url)
    config = Config(str(ROOT / "alembic.ini"))
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(config)
    heads = script.get_heads()
    assert len(heads) == 1
    assert heads[0] == "0016_visual_qa"
