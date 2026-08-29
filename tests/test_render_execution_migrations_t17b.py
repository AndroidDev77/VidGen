"""Migration 0019: the durable execution state T17b added to ``render_jobs``.

The migration is additive and must preserve every existing render job. These
tests prove the chain still has one head, that a job written before the upgrade
survives it with sane execution defaults, that the new constraints are actually
enforced, and that the downgrade removes only what T17b introduced.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import vidgen.db.render_models  # noqa: F401
from vidgen.db.models import Asset, Project, RenderJob

ROOT = Path(__file__).resolve().parents[1]

_EXECUTION_COLUMNS = {
    "claimed_by",
    "claimed_at",
    "lease_expires_at",
    "heartbeat_at",
    "attempt_count",
    "progress_percent",
    "checkpoint",
    "cancel_requested",
    "failure_classification",
    "input_selection",
    "output_sha256",
    "renderer_version",
    "trace_id",
}


def config() -> Config:
    return Config(str(ROOT / "alembic.ini"))


def columns(engine: object, table: str) -> set[str]:
    return {column["name"] for column in inspect(engine).get_columns(table)}  # type: ignore[arg-type]


def test_the_chain_has_exactly_one_head_and_0019_follows_0018() -> None:
    script = ScriptDirectory.from_config(config())
    assert len(script.get_heads()) == 1
    assert script.get_revision("0019_render_execution").down_revision == "0018_final_editorial_qa"


def test_upgrade_downgrade_upgrade_is_clean_and_preserves_render_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = f"sqlite+pysqlite:///{tmp_path / 'execution.db'}"
    monkeypatch.setenv("VIDGEN_DATABASE_URL", url)
    command.upgrade(config(), "0018_final_editorial_qa")
    engine = create_engine(url)
    assert not _EXECUTION_COLUMNS & columns(engine, "render_jobs")

    # A render job that predates T17b, written exactly as 0018 allowed.
    with engine.begin() as connection:
        project_id = uuid4().hex
        connection.execute(
            text(
                "INSERT INTO projects (id, name, owner_subject, status, "
                "target_duration_seconds, visual_style, humor_intensity, settings, "
                "created_at, updated_at) VALUES (:id, 'legacy', 'local-user', 'review', "
                "300, 'flat', 5, '{}', :now, :now)"
            ),
            {"id": project_id, "now": datetime.now(UTC)},
        )
        legacy_id = uuid4().hex
        connection.execute(
            text(
                "INSERT INTO render_jobs (id, project_id, status, attempt, error, "
                "video_profile, audio_profile, caption_profile, pipeline_version, "
                "selected, created_at, updated_at) VALUES (:id, :project, 'render_queued', "
                "1, '{}', '{}', '{}', '{}', 't17/1', 0, :now, :now)"
            ),
            {"id": legacy_id, "project": project_id, "now": datetime.now(UTC)},
        )

    command.upgrade(config(), "head")
    command.check(config())
    assert _EXECUTION_COLUMNS <= columns(engine, "render_jobs")
    with Session(engine) as session:
        job = session.scalars(select(RenderJob)).one()
        # The upgrade leaves an existing job exactly where a queued job belongs:
        # unclaimed, no attempts, no progress, nothing cancelled.
        assert job.status == "render_queued"
        assert job.claimed_by is None and job.lease_expires_at is None
        assert job.attempt_count == 0 and job.progress_percent == 0
        assert job.cancel_requested is False
        assert job.input_selection == {}

    command.downgrade(config(), "0018_final_editorial_qa")
    assert not _EXECUTION_COLUMNS & columns(engine, "render_jobs")
    with Session(engine) as session:
        assert session.execute(text("SELECT count(*) FROM render_jobs")).scalar_one() == 1

    command.upgrade(config(), "head")
    command.check(config())
    assert _EXECUTION_COLUMNS <= columns(engine, "render_jobs")


def test_the_new_constraints_are_enforced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    url = f"sqlite+pysqlite:///{tmp_path / 'constraints.db'}"
    monkeypatch.setenv("VIDGEN_DATABASE_URL", url)
    command.upgrade(config(), "head")
    engine = create_engine(url)
    with Session(engine) as session:
        project = Project(name="fixture", visual_style="test")
        session.add(project)
        session.flush()
        asset = Asset(
            project_id=project.id,
            kind="render",
            sha256="a" * 64,
            byte_size=1,
            media_type="video/mp4",
            storage_key="blob/a",
        )
        session.add(asset)
        session.flush()

        for field, value in (
            ("progress_percent", 101),
            ("progress_percent", -1),
            ("attempt_count", -1),
            ("output_sha256", "too-short"),
        ):
            job = RenderJob(project_id=project.id, status="render_queued")
            setattr(job, field, value)
            session.add(job)
            with pytest.raises(IntegrityError):
                session.flush()
            session.rollback()

        # A completed job must carry its measurements as well as its assets.
        job = RenderJob(
            project_id=project.id,
            status="render_complete",
            manifest_asset_id=asset.id,
            srt_asset_id=asset.id,
            webvtt_asset_id=asset.id,
            final_video_asset_id=asset.id,
            verification_report_asset_id=asset.id,
        )
        session.add(job)
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()
