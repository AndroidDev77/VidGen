"""T21 migration, head, drift, constraint and JSON-Schema checks."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

import vidgen.db  # noqa: F401  - registers every table on Base.metadata
from scripts.export_schemas import rendered_schemas
from tests.review_fixtures import build_project_graph, digest
from vidgen.db.animation_models import AnimationGeneratedVideo
from vidgen.db.repair_models import (
    RepairAttemptRecord,
    RepairDecisionRecord,
    RepairFallbackRender,
    RepairRun,
    VeoOperationRecord,
)

ROOT = Path(__file__).resolve().parents[1]
T21_TABLES = {
    "repair_runs",
    "repair_attempts",
    "repair_decisions",
    "repair_fallback_renders",
    "repair_veo_operations",
}


def config() -> Config:
    return Config(str(ROOT / "alembic.ini"))


def test_t21_follows_t20_in_a_single_headed_chain() -> None:
    script = ScriptDirectory.from_config(config())
    # The chain stays single-headed; later tasks own the head assertion.
    assert len(script.get_heads()) == 1
    assert script.get_revision("0017_repair_fallback").down_revision == "0016_visual_qa"


def test_upgrade_downgrade_upgrade_is_clean_with_no_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = f"sqlite+pysqlite:///{tmp_path / 'repair.db'}"
    monkeypatch.setenv("VIDGEN_DATABASE_URL", url)
    engine = create_engine(url)
    command.upgrade(config(), "head")
    assert T21_TABLES <= set(inspect(engine).get_table_names())
    # No schema drift between the ORM models and the migration chain.
    command.check(config())
    command.downgrade(config(), "0016_visual_qa")
    assert not T21_TABLES & set(inspect(engine).get_table_names())
    command.upgrade(config(), "head")
    assert T21_TABLES <= set(inspect(engine).get_table_names())
    command.check(config())


def test_the_migration_is_additive_and_preserves_existing_tables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = f"sqlite+pysqlite:///{tmp_path / 'additive.db'}"
    monkeypatch.setenv("VIDGEN_DATABASE_URL", url)
    engine = create_engine(url)
    command.upgrade(config(), "0016_visual_qa")
    before = set(inspect(engine).get_table_names())
    # Upgrade to T21 exactly, not to head: later tasks add their own tables and
    # this assertion is about what T21 itself contributes.
    command.upgrade(config(), "0017_repair_fallback")
    after = set(inspect(engine).get_table_names())
    assert before <= after, "T21 removes nothing that T01-T20 created"
    assert after - before == T21_TABLES


def test_no_schema_drift_between_models_and_the_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = f"sqlite+pysqlite:///{tmp_path / 'drift.db'}"
    monkeypatch.setenv("VIDGEN_DATABASE_URL", url)
    command.upgrade(config(), "head")
    inspector = inspect(create_engine(url))
    for model in (
        RepairRun,
        RepairAttemptRecord,
        RepairDecisionRecord,
        RepairFallbackRender,
        VeoOperationRecord,
    ):
        table = model.__table__
        migrated = {column["name"] for column in inspector.get_columns(table.name)}
        assert {column.name for column in table.columns} == migrated


def _graph(session: Session, tmp_path: Path) -> tuple[RepairRun, AnimationGeneratedVideo]:
    graph = build_project_graph(session, blob_root=tmp_path / "blobs")
    video = session.query(AnimationGeneratedVideo).filter_by(shot_id=graph.shot_ids[0]).one()
    from vidgen.db.visual_qa_models import VisualQAAttempt, VisualQAResultRecord, VisualQARun

    qa_run = VisualQARun(
        id=uuid4(),
        project_id=graph.project_id,
        storyboard_run_id=graph.storyboard_run_id,
        shot_id=graph.shot_ids[0],
        shot_workflow_identity="a" * 64,
        target_type="video",
        target_asset_id=video.canonical_asset_id,
        target_asset_sha256="b" * 64,
        qa_identity="c" * 64,
        input_hash="d" * 64,
        idempotency_key="t20:1",
        status="visual_qa_complete",
        importance="normal",
        rubric_version="r",
        sampling_version="s",
        threshold_version="t",
        deterministic_version="d",
        pipeline_version="p",
        deterministic_report={},
        final_outcome="FAIL",
        final_score=40.0,
        pass_threshold=85.0,
        hard_failure=True,
        repair_codes=["WRONG_CHARACTER_IDENTITY"],
        warning_codes=[],
        cost_microusd=0,
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
    )
    attempt = VisualQAAttempt(
        id=uuid4(),
        qa_run_id=qa_run.id,
        attempt_number=1,
        attempt_type="first_pass",
        attempt_identity="e" * 64,
        provider="fake",
        model="fake/1",
        status="completed",
    )
    result = VisualQAResultRecord(
        id=uuid4(),
        qa_run_id=qa_run.id,
        attempt_id=attempt.id,
        outcome="FAIL",
        recomputed_score=40.0,
        pass_threshold=85.0,
        dimension_results=[],
        hard_failure=True,
        hard_failure_codes=["WRONG_CHARACTER_IDENTITY"],
        warning_codes=[],
        repair_codes=["WRONG_CHARACTER_IDENTITY"],
        repair_recommendation="NEW_SEED",
        confidence=0.9,
        canonical=True,
        created_at=datetime.now(UTC),
    )
    session.add_all([qa_run, attempt, result])
    session.flush()
    run = RepairRun(
        id=uuid4(),
        project_id=graph.project_id,
        shot_id=graph.shot_ids[0],
        root_animation_attempt_id=video.id,
        triggering_qa_result_id=result.id,
        policy_version="t21-repair-policy/1.0",
        policy={},
        classifier_version="c/1",
        planner_version="p/1",
        input_hash=digest("repair-input"),
        idempotency_key="t21:1",
        state="REPAIRING",
    )
    session.add(run)
    session.flush()
    return run, video


def _attempt(run: RepairRun, video: AnimationGeneratedVideo, **overrides: object) -> object:
    values: dict[str, object] = {
        "id": uuid4(),
        "repair_run_id": run.id,
        "shot_id": run.shot_id,
        "attempt_ordinal": 0,
        "attempt_kind": "original",
        "attempt_identity": digest(f"attempt-{uuid4()}"),
        "root_animation_attempt_id": video.id,
        "predecessor_attempt_id": None,
        "status": "failed",
    }
    values.update(overrides)
    return RepairAttemptRecord(**values)  # type: ignore[arg-type]


@pytest.fixture
def repair_session(tmp_path: Path) -> Session:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'constraints.db'}")
    from vidgen.db.base import Base

    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as session:
        # SQLite skips foreign keys by default, but it always enforces CHECK and
        # UNIQUE constraints, which is exactly what these tests assert.
        yield session


def test_attempt_ordinals_are_unique_within_a_repair_run(
    repair_session: Session, tmp_path: Path
) -> None:
    run, video = _graph(repair_session, tmp_path)
    repair_session.add(_attempt(run, video, attempt_ordinal=0))
    repair_session.flush()
    repair_session.add(
        _attempt(run, video, attempt_ordinal=0, attempt_identity=digest("collision"))
    )
    with pytest.raises(IntegrityError):
        repair_session.flush()


def test_only_the_root_attempt_may_have_no_predecessor(
    repair_session: Session, tmp_path: Path
) -> None:
    run, video = _graph(repair_session, tmp_path)
    repair_session.add(
        _attempt(
            run,
            video,
            attempt_ordinal=1,
            attempt_kind="same_provider_repair",
            predecessor_attempt_id=None,
        )
    )
    with pytest.raises(IntegrityError):
        repair_session.flush()


def test_an_attempt_may_not_reference_itself(repair_session: Session, tmp_path: Path) -> None:
    run, video = _graph(repair_session, tmp_path)
    identifier = uuid4()
    repair_session.add(
        _attempt(
            run,
            video,
            id=identifier,
            attempt_ordinal=1,
            attempt_kind="same_provider_repair",
            predecessor_attempt_id=identifier,
        )
    )
    with pytest.raises(IntegrityError):
        repair_session.flush()


def test_the_policy_attempt_ceiling_is_enforced_by_the_database(
    repair_session: Session, tmp_path: Path
) -> None:
    run, video = _graph(repair_session, tmp_path)
    root = _attempt(run, video, attempt_ordinal=0)
    repair_session.add(root)
    repair_session.flush()
    repair_session.add(
        _attempt(
            run,
            video,
            attempt_ordinal=5,
            attempt_kind="same_provider_repair",
            predecessor_attempt_id=root.id,  # type: ignore[attr-defined]
        )
    )
    with pytest.raises(IntegrityError):
        repair_session.flush()


def test_selection_requires_a_passing_qa_result(repair_session: Session, tmp_path: Path) -> None:
    run, video = _graph(repair_session, tmp_path)
    root = _attempt(run, video, attempt_ordinal=0)
    repair_session.add(root)
    repair_session.flush()
    repair_session.add(
        _attempt(
            run,
            video,
            attempt_ordinal=1,
            attempt_kind="deterministic_fallback",
            predecessor_attempt_id=root.id,  # type: ignore[attr-defined]
            status="passed",
            selected=True,
            output_qa_result_id=None,
        )
    )
    with pytest.raises(IntegrityError):
        repair_session.flush()


def test_a_project_scoped_idempotency_key_is_unique(
    repair_session: Session, tmp_path: Path
) -> None:
    run, video = _graph(repair_session, tmp_path)
    repair_session.add(
        RepairRun(
            id=uuid4(),
            project_id=run.project_id,
            shot_id=run.shot_id,
            root_animation_attempt_id=video.id,
            triggering_qa_result_id=run.triggering_qa_result_id,
            policy_version="t21-repair-policy/1.0",
            policy={},
            classifier_version="c/1",
            planner_version="p/1",
            input_hash=digest("other-input"),
            idempotency_key=run.idempotency_key,
            state="REPAIRING",
        )
    )
    with pytest.raises(IntegrityError):
        repair_session.flush()


def test_human_review_state_requires_a_structured_reason(
    repair_session: Session, tmp_path: Path
) -> None:
    run, _video = _graph(repair_session, tmp_path)
    run.state = "HUMAN_REVIEW_REQUIRED"
    run.human_review_reason = None
    with pytest.raises(IntegrityError):
        repair_session.flush()


def test_a_locked_run_requires_a_selected_attempt_and_final_qa_result(
    repair_session: Session, tmp_path: Path
) -> None:
    run, _video = _graph(repair_session, tmp_path)
    run.state = "LOCKED"
    with pytest.raises(IntegrityError):
        repair_session.flush()


def test_repair_schemas_are_exported_and_strict() -> None:
    directory = ROOT / "packages" / "contracts" / "schema"
    exported = {path.name for path in directory.iterdir()}
    assert {
        "RepairClassification.v1.json",
        "RepairDiagnostic.v1.json",
        "PromptConstraint.v1.json",
        "PromptDelta.v1.json",
        "RepairPlan.v1.json",
        "RepairPolicy.v1.json",
        "RepairAttempt.v1.json",
        "RepairAttemptLineage.v1.json",
        "RepairDecision.v1.json",
        "RepairOutcome.v1.json",
        "VeoGenerationRequest.v1.json",
        "VeoGenerationResult.v1.json",
        "VeoOperationCheckpoint.v1.json",
        "ParallaxRenderPlan.v1.json",
        "ParallaxRenderManifest.v1.json",
        "ParallaxRenderResult.v1.json",
    } <= exported
    for name in exported:
        if not name.startswith(("Repair", "Parallax", "Veo", "Prompt")):
            continue
        schema = json.loads((directory / name).read_text())
        assert schema.get("additionalProperties") is False, name


def test_exported_json_schemas_are_current() -> None:
    stale = [
        str(path.relative_to(ROOT))
        for path, content in rendered_schemas().items()
        if not path.exists() or path.read_text() != content
    ]
    assert stale == [], "run scripts/export_schemas.py"
