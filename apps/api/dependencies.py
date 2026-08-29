from __future__ import annotations

from collections.abc import Generator
from functools import lru_cache

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from apps.api.settings import get_settings
from vidgen.db.session import build_engine, session_factory
from vidgen.review.workflow_control import (
    FakeWorkflowController,
    TemporalWorkflowController,
    WorkflowController,
)
from vidgen.storage.blob import BlobStore
from vidgen.storage.factory import build_blob_store


@lru_cache
def get_engine() -> Engine:
    return build_engine(get_settings().database_url)


@lru_cache
def get_session_factory() -> sessionmaker[Session]:
    return session_factory(get_engine())


def get_session() -> Generator[Session, None, None]:
    with get_session_factory()() as session:
        yield session


@lru_cache
def get_blob_store() -> BlobStore:
    settings = get_settings()
    return build_blob_store(settings)


@lru_cache
def get_workflow_controller() -> WorkflowController:
    """Return the configured workflow controller.

    Local development and tests use the deterministic fake, so the review UI and
    the API test suite never require a running Temporal cluster.
    """
    settings = get_settings()
    if settings.temporal_use_fake_workflow_controller:
        return FakeWorkflowController()
    return TemporalWorkflowController(
        settings.temporal_target_host,
        settings.temporal_namespace,
        api_key=settings.temporal_api_key,
        tls_enabled=settings.temporal_tls_enabled or settings.temporal_api_key is not None,
    )
