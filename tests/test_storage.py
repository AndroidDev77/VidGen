from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from vidgen.db.base import Base
from vidgen.db.models import Asset, Project
from vidgen.storage.asset_service import AssetService
from vidgen.storage.blob import FilesystemBlobStore


def build_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def test_asset_deduplication_and_provenance(tmp_path: Path) -> None:
    session = build_session()
    project = Project(name="test", visual_style="flat cartoon")
    session.add(project)
    session.flush()
    store = FilesystemBlobStore(tmp_path, b"secret")
    service = AssetService(session, store)
    parent = service.store(
        content=b"source", kind="source_video", media_type="video/mp4", project_id=project.id
    )
    child = service.store(
        content=b"frame",
        kind="frame",
        media_type="image/png",
        project_id=project.id,
        parent_asset_ids=(parent.id,),
        provider="fake",
        provider_request_id="request-1",
        idempotency_key="frame-1",
        generation_parameters={"timestamp": 1.25},
        metadata={"scene": 1},
    )
    duplicate = service.store(
        content=b"frame", kind="frame", media_type="image/png", project_id=project.id
    )
    assert duplicate.id == child.id
    assert duplicate.deduplicated
    persisted = session.get(Asset, child.id)
    assert persisted is not None
    assert persisted.parents[0].id == parent.id
    assert persisted.generation_parameters == {"timestamp": 1.25}
    assert store.read(child.storage_key) == b"frame"


def test_missing_blob_is_recovered_on_deduplicated_write(tmp_path: Path) -> None:
    session = build_session()
    store = FilesystemBlobStore(tmp_path, b"secret")
    service = AssetService(session, store)
    stored = service.store(content=b"recover", kind="json", media_type="application/json")
    (tmp_path / stored.storage_key).unlink()
    recovered = service.store(content=b"recover", kind="json", media_type="application/json")
    assert recovered.deduplicated
    assert store.read(stored.storage_key) == b"recover"


def test_signed_read_url_expires(tmp_path: Path) -> None:
    now = [100.0]
    store = FilesystemBlobStore(tmp_path, b"secret", clock=lambda: now[0])
    store.put_if_absent("sha256/aa/blob", b"payload")
    url = store.signed_read_url("sha256/aa/blob", expires_in_seconds=10)
    assert store.read_signed_url(url) == b"payload"
    now[0] = 110.0
    with pytest.raises(PermissionError, match="expired"):
        store.read_signed_url(url)


def test_blob_key_cannot_escape_root(tmp_path: Path) -> None:
    store = FilesystemBlobStore(tmp_path, b"secret")
    with pytest.raises(ValueError, match="escapes"):
        store.put_if_absent(f"../{uuid4()}", b"bad")
