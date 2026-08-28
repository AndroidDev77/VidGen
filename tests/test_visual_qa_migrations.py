"""T20 migration, head, drift and JSON-Schema checks."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

import vidgen.db  # noqa: F401
from scripts.export_schemas import rendered_schemas
from vidgen.db.visual_qa_models import (
    VisualQAAttempt,
    VisualQAEvidenceRecord,
    VisualQAHumanReview,
    VisualQAResultRecord,
    VisualQARun,
    VisualQASampleRecord,
)

ROOT = Path(__file__).resolve().parents[1]
T20_TABLES = {
    "visual_qa_runs",
    "visual_qa_samples",
    "visual_qa_attempts",
    "visual_qa_results",
    "visual_qa_evidence",
    "visual_qa_human_reviews",
}


def config() -> Config:
    return Config(str(ROOT / "alembic.ini"))


def test_t20_follows_t19_in_a_single_head_chain() -> None:
    script = ScriptDirectory.from_config(config())
    # The chain always has exactly one head; T20 is no longer that head now that
    # T21 follows it, but it still sits directly after T19.
    assert len(script.get_heads()) == 1
    assert script.get_revision("0016_visual_qa").down_revision == "0015_continuity_references"


def test_upgrade_downgrade_upgrade_is_clean_with_no_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = f"sqlite+pysqlite:///{tmp_path / 'visual-qa.db'}"
    monkeypatch.setenv("VIDGEN_DATABASE_URL", url)
    engine = create_engine(url)
    command.upgrade(config(), "0016_visual_qa")
    assert T20_TABLES <= set(inspect(engine).get_table_names())
    # No schema drift between the ORM models and the migration chain.
    command.check(config())
    command.downgrade(config(), "0015_continuity_references")
    assert not T20_TABLES & set(inspect(engine).get_table_names())
    command.upgrade(config(), "0016_visual_qa")
    assert T20_TABLES <= set(inspect(engine).get_table_names())


def test_the_migration_is_additive_and_preserves_existing_tables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = f"sqlite+pysqlite:///{tmp_path / 'additive.db'}"
    monkeypatch.setenv("VIDGEN_DATABASE_URL", url)
    engine = create_engine(url)
    command.upgrade(config(), "0015_continuity_references")
    before = set(inspect(engine).get_table_names())
    command.upgrade(config(), "0016_visual_qa")
    after = set(inspect(engine).get_table_names())
    assert before <= after, "T20 removes nothing that T01-T19 created"
    assert after - before == T20_TABLES


def test_downgrade_refuses_to_destroy_recorded_qa_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime

    from tests.review_fixtures import build_project_graph

    url = f"sqlite+pysqlite:///{tmp_path / 'destructive.db'}"
    monkeypatch.setenv("VIDGEN_DATABASE_URL", url)
    command.upgrade(config(), "head")
    engine = create_engine(url)
    with sessionmaker(bind=engine)() as session:
        graph = build_project_graph(session, blob_root=tmp_path / "blobs")
        run = VisualQARun(
            id=uuid4(),
            project_id=graph.project_id,
            storyboard_run_id=graph.storyboard_run_id,
            shot_id=graph.shot_ids[0],
            shot_workflow_identity="a" * 64,
            target_type="video",
            target_asset_id=graph.keyframe_asset_ids[0],
            target_asset_sha256="b" * 64,
            qa_identity="c" * 64,
            input_hash="d" * 64,
            idempotency_key="t20:1",
            status="visual_qa_complete",
            importance="normal",
            rubric_version="visual-qa-rubric/1.0",
            sampling_version="visual-qa-sampler/1.0",
            threshold_version="visual-qa-thresholds/1.0",
            deterministic_version="visual-qa-deterministic/1.0",
            pipeline_version="visual-qa/1.0.0",
            deterministic_report={},
            final_outcome="FAIL",
            final_score=40.0,
            pass_threshold=85.0,
            hard_failure=True,
            repair_recommendation="NEW_SEED",
            repair_codes=["WRONG_CHARACTER_IDENTITY"],
            warning_codes=[],
            cost_microusd=0,
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
        )
        session.add(run)
        session.commit()

    with pytest.raises(RuntimeError, match="unsafe T20 downgrade"):
        command.downgrade(config(), "0015_continuity_references")
    assert "visual_qa_runs" in set(inspect(engine).get_table_names())


def test_no_schema_drift_between_models_and_the_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = f"sqlite+pysqlite:///{tmp_path / 'drift.db'}"
    monkeypatch.setenv("VIDGEN_DATABASE_URL", url)
    command.upgrade(config(), "head")
    inspector = inspect(create_engine(url))
    for model in (
        VisualQARun,
        VisualQASampleRecord,
        VisualQAAttempt,
        VisualQAResultRecord,
        VisualQAEvidenceRecord,
        VisualQAHumanReview,
    ):
        table = model.__table__
        migrated = {column["name"] for column in inspector.get_columns(table.name)}
        assert {column.name for column in table.columns} == migrated


def test_exported_json_schemas_are_current() -> None:
    stale = [
        str(path.relative_to(ROOT))
        for path, content in rendered_schemas().items()
        if not path.exists() or path.read_text() != content
    ]
    assert stale == [], "run scripts/export_schemas.py"


def test_visual_qa_schemas_are_exported_and_strict() -> None:
    directory = ROOT / "packages" / "contracts" / "schema"
    exported = {path.name for path in directory.glob("VisualQA*.v1.json")}
    assert {
        "VisualQAResult.v1.json",
        "VisualQAScore.v1.json",
        "VisualQAProviderRequest.v1.json",
        "VisualQAProviderResult.v1.json",
        "VisualQASamplingManifest.v1.json",
        "VisualQARubric.v1.json",
        "VisualQAThresholds.v1.json",
        "VisualQAFailure.v1.json",
    } <= exported
    for name in exported:
        schema = json.loads((directory / name).read_text())
        assert schema.get("additionalProperties") is False, name
