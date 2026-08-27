from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from pytest import MonkeyPatch
from sqlalchemy import create_engine, inspect

ROOT = Path(__file__).resolve().parents[1]
TABLES = {"image_generation_runs", "image_generation_items", "generated_keyframe_images"}


def config() -> Config:
    return Config(str(ROOT / "alembic.ini"))


def test_t14_is_the_only_head_and_follows_storyboard() -> None:
    script = ScriptDirectory.from_config(config())
    assert script.get_heads() == ["0011_image_generation"]
    assert script.get_revision("0011_image_generation").down_revision == "0010_storyboard"


def test_t14_upgrade_downgrade_upgrade(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    url = f"sqlite+pysqlite:///{tmp_path / 't14.db'}"
    monkeypatch.setenv("VIDGEN_DATABASE_URL", url)
    engine = create_engine(url)
    command.upgrade(config(), "head")
    assert TABLES <= set(inspect(engine).get_table_names())
    command.downgrade(config(), "0010_storyboard")
    assert not TABLES & set(inspect(engine).get_table_names())
    command.upgrade(config(), "head")
    assert TABLES <= set(inspect(engine).get_table_names())
