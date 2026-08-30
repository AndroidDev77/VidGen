from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Generator, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import vidgen.db.models
import vidgen.db.script_models
import vidgen.db.upload_models  # noqa: F401
from apps.api.dependencies import get_blob_store, get_session
from apps.api.main import create_app
from apps.api.settings import APISettings, get_settings
from tests.client_fixtures import ReviewClient, review_client_context
from vidgen.db.base import Base
from vidgen.storage.blob import FilesystemBlobStore


@pytest.fixture
def golden_video(tmp_path: Path) -> Path:
    output = tmp_path / "golden.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=320x180:d=1:r=30",
            "-f",
            "lavfi",
            "-i",
            "color=c=white:s=320x180:d=1:r=30",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=320x180:d=1:r=30",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=3",
            "-filter_complex",
            "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
            "-map",
            "[v]",
            "-map",
            "3:a",
            "-c:v",
            "mpeg4",
            "-q:v",
            "3",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(output),
        ],
        check=True,
        capture_output=True,
    )
    assert output.stat().st_size > 1024
    return output


@pytest.fixture
def golden_transcription_audio(tmp_path: Path) -> Path:
    output = tmp_path / "transcription.wav"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2:sample_rate=16000",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=16000:cl=mono:d=0.5",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=660:duration=3.5:sample_rate=16000",
            "-filter_complex",
            "[0:a][1:a][2:a]concat=n=3:v=0:a=1[out]",
            "-map",
            "[out]",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(output),
        ],
        check=True,
    )
    return output


@pytest.fixture
def api_client(tmp_path: Path) -> Generator[tuple[TestClient, sessionmaker[Session]], None, None]:
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'api.db'}",
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
    app = create_app()

    def session_override() -> Generator[Session, None, None]:
        with factory() as session:
            yield session

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_blob_store] = lambda: blob_store
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app) as client:
        yield client, factory


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def review_client(tmp_path: Path) -> Iterator[ReviewClient]:
    with review_client_context(tmp_path) as client:
        yield client
