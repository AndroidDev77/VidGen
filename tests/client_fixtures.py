"""The T18 control-plane test client.

It lives outside ``conftest.py`` so a test module can import it directly: a
test that has to seed the control plane's database before the application
opens an engine on it needs to order the two itself.
"""

from __future__ import annotations

from collections.abc import Generator, Iterator
from contextlib import contextmanager
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from apps.api.dependencies import (
    get_blob_store,
    get_session,
    get_session_factory,
    get_workflow_controller,
)
from apps.api.main import create_app
from apps.api.settings import APISettings, get_settings
from vidgen.db.base import Base
from vidgen.review.workflow_control import FakeWorkflowController
from vidgen.storage.blob import FilesystemBlobStore

ReviewClient = tuple[TestClient, sessionmaker[Session], FakeWorkflowController]


@contextmanager
def review_client_context(tmp_path: Path) -> Iterator[ReviewClient]:
    """A T18 control-plane client backed by SQLite and a deterministic controller.

    Exposed as a context manager as well as a fixture so a test that has to
    seed ``tmp_path / "review.db"`` first can order the two itself.
    """
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'review.db'}",
        connect_args={"check_same_thread": False},
    )
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
    controller = FakeWorkflowController()
    app = create_app()

    def session_override() -> Generator[Session, None, None]:
        with factory() as session:
            yield session

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_session_factory] = lambda: factory
    app.dependency_overrides[get_blob_store] = lambda: blob_store
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_workflow_controller] = lambda: controller
    with TestClient(app) as client:
        yield client, factory, controller
