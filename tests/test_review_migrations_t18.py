"""T18 migration is a single head after the T17 render head."""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

ROOT = Path(__file__).resolve().parents[1]
T18_TABLES = {
    "resource_versions",
    "api_idempotency_records",
    "project_ui_events",
    "render_approvals",
    "downstream_invalidations",
}


def config() -> Config:
    return Config(str(ROOT / "alembic.ini"))


def test_t18_is_the_only_head_and_follows_t17() -> None:
    script = ScriptDirectory.from_config(config())
    # The chain always has exactly one head; its name moves with each
    # roadmap task, so assert the invariant rather than the current name.
    assert len(script.get_heads()) == 1
    assert script.get_revision("0014_review_ui").down_revision == "0013_render"


def test_t18_upgrade_downgrade_upgrade_is_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = f"sqlite+pysqlite:///{tmp_path / 'review.db'}"
    monkeypatch.setenv("VIDGEN_DATABASE_URL", url)
    engine = create_engine(url)
    command.upgrade(config(), "head")
    assert T18_TABLES <= set(inspect(engine).get_table_names())
    # No schema drift between the models and the migration chain.
    command.check(config())
    command.downgrade(config(), "0013_render")
    assert not T18_TABLES & set(inspect(engine).get_table_names())
    command.upgrade(config(), "head")
    assert T18_TABLES <= set(inspect(engine).get_table_names())


def test_downgrade_refuses_to_destroy_recorded_approvals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime
    from uuid import uuid4

    from sqlalchemy.orm import sessionmaker

    from tests.review_fixtures import build_project_graph
    from vidgen.db.review_models import RenderApproval

    url = f"sqlite+pysqlite:///{tmp_path / 'approvals.db'}"
    monkeypatch.setenv("VIDGEN_DATABASE_URL", url)
    monkeypatch.delenv("VIDGEN_ALLOW_DESTRUCTIVE_MIGRATION_TEST", raising=False)
    command.upgrade(config(), "head")
    engine = create_engine(url)
    with sessionmaker(bind=engine)() as session:
        graph = build_project_graph(session, owner_subject="owner-a")
        assert graph.render_job_id is not None
        session.add(
            RenderApproval(
                id=uuid4(),
                project_id=graph.project_id,
                render_job_id=graph.render_job_id,
                approved_by="owner-a",
                lineage_hash="a" * 64,
                approved_at=datetime.now(UTC),
            )
        )
        session.commit()
    with pytest.raises(RuntimeError, match="render approval record"):
        command.downgrade(config(), "0013_render")
