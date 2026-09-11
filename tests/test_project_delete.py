"""Deleting a project removes every row the pipeline recorded for it.

The suite's other SQLite engines leave ``PRAGMA foreign_keys`` at its default
of OFF, which would let any deletion order pass. These tests turn it on, so the
database enforces the same ``RESTRICT`` constraints Postgres does and the walk
in ``services.projects.deletion`` is actually under test.
"""

from __future__ import annotations

from collections.abc import Generator, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, event, func, select
from sqlalchemy.orm import Session, sessionmaker

from apps.api.dependencies import (
    get_blob_store,
    get_session,
    get_session_factory,
    get_workflow_controller,
)
from apps.api.main import create_app
from apps.api.settings import APISettings, get_settings
from services.projects.deletion import _deletion_plan, _foreign_keys
from tests.publication_fixtures import PublishableProject, build_publishable_project
from vidgen.db.base import Base
from vidgen.db.models import Asset, SourceVideo
from vidgen.review.workflow_control import FakeWorkflowController
from vidgen.storage.blob import FilesystemBlobStore

DeleteClient = tuple[TestClient, sessionmaker[Session], FilesystemBlobStore]

#: Tables that belong to the deployment rather than to any one project. A
#: project delete must leave them alone.
GLOBAL_TABLES = frozenset(
    {
        "api_idempotency_records",
        "pricing_versions",
        "provider_price_rates",
        "youtube_connections",
        "youtube_connection_secrets",
        "youtube_oauth_states",
    }
)


@contextmanager
def _client(tmp_path: Path) -> Iterator[DeleteClient]:
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'delete.db'}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _enforce_foreign_keys(connection: Any, _record: Any) -> None:
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    blob_store = FilesystemBlobStore(tmp_path / "blobs", b"test-secret")
    settings = APISettings(
        database_url=str(engine.url),
        blob_root=tmp_path / "blobs",
        upload_root=tmp_path / "uploads",
        signing_secret="test-secret",
        max_upload_bytes=32 * 1024 * 1024,
    )
    app = create_app()

    def session_override() -> Generator[Session, None, None]:
        with factory() as session:
            yield session

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_session_factory] = lambda: factory
    app.dependency_overrides[get_blob_store] = lambda: blob_store
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_workflow_controller] = lambda: FakeWorkflowController()
    try:
        with TestClient(app) as client:
            yield client, factory, blob_store
    finally:
        engine.dispose()


@pytest.fixture
def delete_client(tmp_path: Path) -> Iterator[DeleteClient]:
    with _client(tmp_path) as client:
        yield client


def _seed(
    factory: sessionmaker[Session], store: FilesystemBlobStore, name: str = "Season 3 Episode 4"
) -> PublishableProject:
    with factory() as session:
        project = build_publishable_project(session, store, name=name)
        session.commit()
    return project


def _populated_tables(factory: sessionmaker[Session]) -> dict[str, int]:
    """Row counts per table, skipping the empty ones."""
    with factory() as session:
        counts = {
            table.name: session.scalar(select(func.count()).select_from(table)) or 0
            for table in Base.metadata.sorted_tables
        }
    return {name: count for name, count in counts.items() if count}


def _project_row_counts(factory: sessionmaker[Session], project_id: UUID) -> dict[str, int | None]:
    with factory() as session:
        return {
            table.name: session.scalar(
                select(func.count()).select_from(table).where(table.c.project_id == project_id)
            )
            for table in Base.metadata.sorted_tables
            if "project_id" in table.c
        }


def _blob_keys(factory: sessionmaker[Session], project_id: UUID) -> list[str]:
    with factory() as session:
        return list(
            session.scalars(select(Asset.storage_key).where(Asset.project_id == project_id))
        )


def _stored(store: FilesystemBlobStore, key: str) -> bool:
    return (store.root / key).exists()


def test_delete_removes_every_row_of_a_full_pipeline_graph(delete_client: DeleteClient) -> None:
    client, factory, store = delete_client
    project = _seed(factory, store)
    before = _populated_tables(factory)
    # The fixture only earns its keep as a regression test if it reaches the
    # tables whose RESTRICT foreign keys used to block the delete.
    assert {
        "assets",
        "render_jobs",
        "render_approvals",
        "final_editorial_runs",
        "scripts",
        "narration_runs",
        "storyboard_runs",
    } <= set(before)

    response = client.delete(f"/api/v1/projects/{project.project_id}")

    assert response.status_code == 204
    assert set(_populated_tables(factory)) <= GLOBAL_TABLES


def test_delete_removes_the_projects_blobs(delete_client: DeleteClient) -> None:
    client, factory, store = delete_client
    project = _seed(factory, store)
    keys = _blob_keys(factory, project.project_id)
    assert keys and all(_stored(store, key) for key in keys)

    assert client.delete(f"/api/v1/projects/{project.project_id}").status_code == 204

    assert not any(_stored(store, key) for key in keys)


def test_delete_leaves_another_project_untouched(delete_client: DeleteClient) -> None:
    client, factory, store = delete_client
    doomed = _seed(factory, store, name="Doomed")
    kept = _seed(factory, store, name="Kept")
    kept_keys = _blob_keys(factory, kept.project_id)
    kept_counts = _project_row_counts(factory, kept.project_id)
    # The two projects hold identical synthetic media, so content-addressed
    # storage hands them the same keys: deleting one must not empty the other.
    assert set(kept_keys) & set(_blob_keys(factory, doomed.project_id))

    assert client.delete(f"/api/v1/projects/{doomed.project_id}").status_code == 204

    assert _project_row_counts(factory, kept.project_id) == kept_counts
    assert all(_stored(store, key) for key in kept_keys)
    assert client.get(f"/api/v1/projects/{kept.project_id}").status_code == 200


def test_delete_refused_by_an_outside_reference_changes_nothing(
    delete_client: DeleteClient,
) -> None:
    client, factory, store = delete_client
    doomed = _seed(factory, store, name="Doomed")
    other = _seed(factory, store, name="Other")
    # source_videos.asset_id is RESTRICT: another project holding one of these
    # assets is data still in use, and the delete has to say so rather than
    # orphan it.
    with factory() as session:
        borrowed = session.scalars(
            select(Asset.id).where(
                Asset.project_id == doomed.project_id,
                Asset.id.not_in(select(SourceVideo.asset_id)),
            )
        ).first()
        assert borrowed is not None
        session.add(
            SourceVideo(
                project_id=other.project_id,
                asset_id=borrowed,
                filename="borrowed.mp4",
                duration_seconds=1.0,
                probe={},
            )
        )
        session.commit()
    keys = _blob_keys(factory, doomed.project_id)
    before = _populated_tables(factory)

    response = client.delete(f"/api/v1/projects/{doomed.project_id}")

    assert response.status_code == 409
    assert response.json()["detail_code"] == "project_has_references"
    assert _populated_tables(factory) == before
    assert all(_stored(store, key) for key in keys)


def test_foreign_keys_are_enforced(delete_client: DeleteClient) -> None:
    """The guard for every test above: without the pragma they prove nothing."""
    _, factory, _ = delete_client
    with factory() as session:
        engine = session.get_bind()
    assert isinstance(engine, Engine)
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1


def test_the_whole_schema_can_be_ordered_children_first() -> None:
    """Every table, not just the ones a fixture happens to populate.

    A stage that adds a table with a RESTRICT foreign key is the failure this
    guards: the plan has to place it before its parents, or clear a nullable
    column to break the cycle it closed.
    """
    scope = {table.name: {uuid4()} for table in Base.metadata.sorted_tables}

    order, cleared = _deletion_plan(scope)

    assert {table.name for table in order} == set(scope)
    assert all(column.nullable for _, column in cleared)
    broken = {(table.name, column.name) for table, column in cleared}
    position = {table.name: index for index, table in enumerate(order)}
    referenced_too_early = [
        (table.name, column.name, parent.name)
        for table in order
        for column, parent in _foreign_keys(table)
        if (table.name, column.name) not in broken
        and parent.name != table.name
        and position[parent.name] < position[table.name]
    ]
    assert referenced_too_early == []


def test_a_scope_of_one_table_is_ordered_on_its_own() -> None:
    order, cleared = _deletion_plan({"projects": {uuid4()}})

    assert [table.name for table in order] == ["projects"]
    assert cleared == []
