"""T22 migration, head, drift, constraint and JSON-Schema checks."""

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

import vidgen.db
from scripts.export_schemas import rendered_schemas
from tests.review_fixtures import build_project_graph, digest
from vidgen.db.final_editorial_models import (
    FinalCompletionGate,
    FinalEditorialCheckRecord,
    FinalEditorialReview,
    FinalEditorialRun,
)

ROOT = Path(__file__).resolve().parents[1]
T22_TABLES = {
    "final_editorial_runs",
    "final_editorial_checks",
    "final_editorial_provider_attempts",
    "final_editorial_reviews",
    "final_completion_gates",
}


def config() -> Config:
    return Config(str(ROOT / "alembic.ini"))


def test_t22_follows_t21_on_a_single_head_chain() -> None:
    """One head, and T22 still sits directly on T21.

    The head moves with each roadmap task, so the invariant to assert is that
    there is exactly one of them - not that it is still T22's revision.
    """
    script = ScriptDirectory.from_config(config())
    assert len(script.get_heads()) == 1
    assert script.get_revision("0018_final_editorial_qa").down_revision == "0017_repair_fallback"


def test_upgrade_downgrade_upgrade_is_clean_with_no_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = f"sqlite+pysqlite:///{tmp_path / 'final.db'}"
    monkeypatch.setenv("VIDGEN_DATABASE_URL", url)
    engine = create_engine(url)
    command.upgrade(config(), "head")
    assert T22_TABLES <= set(inspect(engine).get_table_names())
    # No schema drift between the ORM models and the migration chain.
    command.check(config())
    command.downgrade(config(), "0017_repair_fallback")
    assert not T22_TABLES & set(inspect(engine).get_table_names())
    command.upgrade(config(), "head")
    assert T22_TABLES <= set(inspect(engine).get_table_names())
    command.check(config())


def test_the_migration_is_additive_and_preserves_existing_tables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = f"sqlite+pysqlite:///{tmp_path / 'additive.db'}"
    monkeypatch.setenv("VIDGEN_DATABASE_URL", url)
    engine = create_engine(url)
    command.upgrade(config(), "0017_repair_fallback")
    before = set(inspect(engine).get_table_names())
    # Upgrade to T22 exactly, not to head: later tasks add their own tables and
    # this assertion is about what T22 itself contributes.
    command.upgrade(config(), "0018_final_editorial_qa")
    after = set(inspect(engine).get_table_names())
    assert before <= after, "T22 removes nothing that T01-T21 created"
    assert after - before == T22_TABLES


def test_the_downgrade_refuses_to_destroy_final_qa_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = f"sqlite+pysqlite:///{tmp_path / 'provenance.db'}"
    monkeypatch.setenv("VIDGEN_DATABASE_URL", url)
    engine = create_engine(url)
    command.upgrade(config(), "head")
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        graph = build_project_graph(session)
        session.add(_run(graph))
        session.commit()
    with pytest.raises(RuntimeError, match="final editorial-QA provenance"):
        command.downgrade(config(), "0017_repair_fallback")
    assert T22_TABLES <= set(inspect(engine).get_table_names())


def _run(graph: object, **overrides: object) -> FinalEditorialRun:
    payload: dict[str, object] = {
        "project_id": graph.project_id,  # type: ignore[attr-defined]
        "render_job_id": graph.render_job_id,  # type: ignore[attr-defined]
        "final_render_asset_id": graph.final_video_asset_id,  # type: ignore[attr-defined]
        "render_manifest_asset_id": graph.final_video_asset_id,  # type: ignore[attr-defined]
        "render_identity": digest("render"),
        "final_qa_identity": digest("identity"),
        "input_hash": digest("input"),
        "configuration_hash": digest("configuration"),
        "idempotency_key": "t22:1",
        "status": "FINAL_QA_PASSED",
        "current_phase": "COMPLETION_GATE",
        "completed_phases": ["INPUT_VALIDATION"],
        "pipeline_version": "final-editorial/1.0.0",
        "gate_version": "final-gate/1.0",
    }
    payload.update(overrides)
    return FinalEditorialRun(**payload)


@pytest.fixture
def factory(tmp_path: Path) -> sessionmaker[Session]:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'constraints.db'}")
    vidgen.db.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def test_a_pass_can_never_be_recorded_with_a_blocking_finding(
    factory: sessionmaker[Session],
) -> None:
    with factory() as session:
        graph = build_project_graph(session)
        session.add(
            _run(graph, final_decision="PASS", blocking_finding_count=1, report_asset_id=None)
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_a_pass_can_never_be_recorded_with_an_unresolved_review(
    factory: sessionmaker[Session],
) -> None:
    with factory() as session:
        graph = build_project_graph(session)
        session.add(_run(graph, final_decision="PASS", review_finding_count=2))
        with pytest.raises(IntegrityError):
            session.commit()


def test_a_fail_must_name_at_least_one_confirmed_blocking_issue(
    factory: sessionmaker[Session],
) -> None:
    with factory() as session:
        graph = build_project_graph(session)
        session.add(_run(graph, final_decision="FAIL"))
        with pytest.raises(IntegrityError):
            session.commit()


def test_only_one_selected_report_may_exist_per_render(
    factory: sessionmaker[Session],
) -> None:
    with factory() as session:
        graph = build_project_graph(session)
        first = _run(
            graph,
            final_decision="PASS",
            selected=True,
            report_asset_id=graph.final_video_asset_id,
        )
        session.add(first)
        session.commit()
        session.add(
            _run(
                graph,
                final_qa_identity=digest("identity-2"),
                idempotency_key="t22:2",
                final_decision="PASS",
                selected=True,
                report_asset_id=graph.final_video_asset_id,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_a_selected_run_must_carry_a_decision_and_a_persisted_report(
    factory: sessionmaker[Session],
) -> None:
    with factory() as session:
        graph = build_project_graph(session)
        session.add(_run(graph, selected=True))
        with pytest.raises(IntegrityError):
            session.commit()


def test_project_scoped_idempotency_is_enforced(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        graph = build_project_graph(session)
        session.add(_run(graph))
        session.commit()
        session.add(_run(graph, final_qa_identity=digest("identity-3")))
        with pytest.raises(IntegrityError):
            session.commit()


def test_a_blocking_check_must_carry_evidence(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        graph = build_project_graph(session)
        run = _run(graph)
        session.add(run)
        session.commit()
        session.add(
            FinalEditorialCheckRecord(
                final_editorial_run_id=run.id,
                check_key=uuid4(),
                check_type="media",
                check_code="VIDEO_DECODE_FAILURE",
                check_version="final-deterministic/1.0",
                status="fail",
                blocking=True,
                evidence_references=[],
                evidence_count=0,
                created_at=datetime.now(UTC),
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_a_failed_check_is_always_blocking(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        graph = build_project_graph(session)
        run = _run(graph)
        session.add(run)
        session.commit()
        session.add(
            FinalEditorialCheckRecord(
                final_editorial_run_id=run.id,
                check_key=uuid4(),
                check_type="audio",
                check_code="LOUDNESS_OUT_OF_RANGE",
                check_version="final-audio/1.0",
                status="fail",
                blocking=False,
                evidence_references=[{"code": "LOUDNESS_OUT_OF_RANGE"}],
                evidence_count=1,
                created_at=datetime.now(UTC),
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_a_human_review_always_carries_a_structured_reason(
    factory: sessionmaker[Session],
) -> None:
    with factory() as session:
        graph = build_project_graph(session)
        run = _run(graph)
        session.add(run)
        session.commit()
        session.add(
            FinalEditorialReview(
                final_editorial_run_id=run.id,
                finding_id=uuid4(),
                reviewer_subject="owner-a",
                decision="accept",
                reason_code="reviewed",
                reason="",
                expected_row_version=1,
                created_at=datetime.now(UTC),
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_one_finding_may_only_be_adjudicated_once(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        graph = build_project_graph(session)
        run = _run(graph)
        session.add(run)
        session.commit()
        finding_id = uuid4()
        for decision in ("accept", "reject"):
            session.add(
                FinalEditorialReview(
                    final_editorial_run_id=run.id,
                    finding_id=finding_id,
                    reviewer_subject="owner-a",
                    decision=decision,
                    reason_code="reviewed",
                    reason="a structured reason",
                    expected_row_version=1,
                    created_at=datetime.now(UTC),
                )
            )
        with pytest.raises(IntegrityError):
            session.commit()


def test_a_completion_gate_cannot_pass_over_an_unresolved_blocker(
    factory: sessionmaker[Session],
) -> None:
    with factory() as session:
        graph = build_project_graph(session)
        run = _run(graph)
        session.add(run)
        session.commit()
        session.add(
            FinalCompletionGate(
                project_id=graph.project_id,
                final_editorial_run_id=run.id,
                final_render_asset_id=graph.final_video_asset_id,
                render_identity=digest("render"),
                decision="PASS",
                blocking_finding_count=1,
                gate_version="final-gate/1.0",
                created_at=datetime.now(UTC),
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_the_exported_json_schemas_are_current() -> None:
    stale = [
        path.name
        for path, content in rendered_schemas().items()
        if "Final" in path.name and (not path.exists() or path.read_text() != content)
    ]
    assert not stale, f"stale T22 contract schemas: {stale}"


def test_every_public_t22_contract_has_an_exported_schema() -> None:
    exported = {path.name for path in rendered_schemas()}
    for name in (
        "FinalQAInput",
        "FinalQAConfiguration",
        "FinalDeterministicCheck",
        "FinalMediaMeasurements",
        "FinalAudioCheck",
        "FinalCaptionCheck",
        "FinalEditorialDimension",
        "FinalEditorialFinding",
        "FinalEditorialEvidence",
        "FinalEditorialProviderRequest",
        "FinalEditorialProviderResult",
        "FinalEditorialAdjudication",
        "FinalRemediationRoute",
        "FinalEditorialReport",
        "FinalGateDecision",
        "FinalEditorialResult",
    ):
        assert f"{name}.v1.json" in exported


def test_the_report_schema_forbids_unknown_fields() -> None:
    schema = json.loads(
        (ROOT / "packages" / "contracts" / "schema" / "FinalEditorialReport.v1.json").read_text()
    )
    assert schema["additionalProperties"] is False
    definitions = schema.get("$defs", {})
    for name, definition in definitions.items():
        if definition.get("type") == "object":
            assert definition.get("additionalProperties") is False, name
