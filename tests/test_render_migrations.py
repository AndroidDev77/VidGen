from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import vidgen.db.render_models  # noqa: F401
from vidgen.db.models import Project, RenderJob

ROOT = Path(__file__).resolve().parents[1]


def config() -> Config:
    return Config(str(ROOT / "alembic.ini"))


def test_t17_is_single_head_and_upgrade_downgrade_upgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = ScriptDirectory.from_config(config())
    assert script.get_heads() == ["0014_review_ui"]
    assert script.get_revision("0013_render").down_revision == "0012_animation"
    url = f"sqlite+pysqlite:///{tmp_path / 'render.db'}"
    monkeypatch.setenv("VIDGEN_DATABASE_URL", url)
    command.upgrade(config(), "head")
    engine = create_engine(url)
    assert {"render_jobs", "render_attempts", "caption_tracks"} <= set(
        inspect(engine).get_table_names()
    )
    command.check(config())
    command.downgrade(config(), "0012_animation")
    assert not {"render_attempts", "caption_tracks"} & set(inspect(engine).get_table_names())
    command.upgrade(config(), "head")
    command.check(config())


def test_render_identity_and_completed_asset_constraints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = f"sqlite+pysqlite:///{tmp_path / 'constraints.db'}"
    monkeypatch.setenv("VIDGEN_DATABASE_URL", url)
    command.upgrade(config(), "head")
    engine = create_engine(url)
    with Session(engine) as session:
        project = Project(name="fixture", visual_style="test")
        session.add(project)
        session.flush()
        first = RenderJob(
            project_id=project.id,
            status="render_queued",
            render_identity="a" * 64,
            idempotency_key="same",
        )
        session.add(first)
        session.commit()
        session.add(
            RenderJob(
                project_id=project.id,
                status="render_queued",
                render_identity="a" * 64,
                idempotency_key="other",
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
        session.add(
            RenderJob(
                project_id=project.id,
                status="render_complete",
                render_identity="b" * 64,
                idempotency_key="complete",
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
