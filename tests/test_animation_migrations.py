from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from pytest import MonkeyPatch
from sqlalchemy import create_engine, inspect

ROOT = Path(__file__).resolve().parents[1]
TABLES = {"animation_runs", "animation_items", "runway_tasks", "animation_generated_videos"}


def config() -> Config:
    return Config(str(ROOT / "alembic.ini"))


def test_t15_is_the_only_head_and_follows_t14() -> None:
    script = ScriptDirectory.from_config(config())
    assert script.get_heads() == ["0012_animation"]
    assert script.get_revision("0012_animation").down_revision == "0011_image_generation"


def test_t15_upgrade_downgrade_upgrade(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    url = f"sqlite+pysqlite:///{tmp_path / 't15.db'}"
    monkeypatch.setenv("VIDGEN_DATABASE_URL", url)
    engine = create_engine(url)
    command.upgrade(config(), "head")
    assert TABLES <= set(inspect(engine).get_table_names())
    command.downgrade(config(), "0011_image_generation")
    assert not TABLES & set(inspect(engine).get_table_names())
    command.upgrade(config(), "head")
    assert TABLES <= set(inspect(engine).get_table_names())
