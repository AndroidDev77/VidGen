from __future__ import annotations

from collections.abc import Generator
from functools import lru_cache

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from apps.api.settings import get_settings
from vidgen.db.session import build_engine, session_factory
from vidgen.storage.blob import FilesystemBlobStore


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
def get_blob_store() -> FilesystemBlobStore:
    settings = get_settings()
    return FilesystemBlobStore(settings.blob_root, settings.signing_secret.encode())
