from __future__ import annotations

import hashlib
import math
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from vidgen.db.models import Asset, SourceVideo
from vidgen.db.upload_models import UploadPart, UploadSession


def create_project(client: TestClient) -> dict[str, Any]:
    response = client.post(
        "/api/v1/projects",
        json={
            "name": "Golden episode",
            "target_duration_seconds": 300,
            "visual_style": "flat cartoon",
            "humor_intensity": 6,
        },
    )
    assert response.status_code == 201
    return response.json()


def initialize(
    client: TestClient, project_id: str, content: bytes, **overrides: Any
) -> dict[str, Any]:
    payload = {
        "filename": "golden.mp4",
        "media_type": "video/mp4",
        "expected_size": len(content),
        "expected_sha256": hashlib.sha256(content).hexdigest(),
        "part_size": 4096,
        **overrides,
    }
    response = client.post(f"/api/v1/projects/{project_id}/uploads", json=payload)
    assert response.status_code == 201
    return response.json()


def test_project_api_and_resumable_upload(
    api_client: tuple[TestClient, sessionmaker[Session]], golden_video: Path
) -> None:
    client, factory = api_client
    project = create_project(client)
    assert client.get("/api/v1/projects").json()[0]["id"] == project["id"]
    content = golden_video.read_bytes()
    upload = initialize(client, project["id"], content)
    part_size = upload["part_size"]
    parts = [content[index : index + part_size] for index in range(0, len(content), part_size)]

    for part_number in reversed(range(len(parts))):
        response = client.put(
            f"/api/v1/uploads/{upload['id']}/parts/{part_number}",
            content=parts[part_number],
        )
        assert response.status_code == 200
        assert response.json()["duplicate"] is False

    with factory() as session:
        stored_part = session.scalar(
            select(UploadPart).where(
                UploadPart.upload_id == UUID(upload["id"]), UploadPart.part_number == 0
            )
        )
        assert stored_part is not None
        Path(stored_part.storage_path).unlink()

    duplicate = client.put(f"/api/v1/uploads/{upload['id']}/parts/0", content=parts[0])
    assert duplicate.status_code == 200
    assert duplicate.json()["duplicate"] is True
    conflict = client.put(f"/api/v1/uploads/{upload['id']}/parts/0", content=b"different")
    assert conflict.status_code == 409
    assert conflict.json()["detail_code"] == "conflicting_part"

    complete = client.post(f"/api/v1/uploads/{upload['id']}/complete")
    assert complete.status_code == 200
    result = complete.json()
    assert result["sha256"] == hashlib.sha256(content).hexdigest()
    assert result["byte_size"] == len(content)
    assert client.post(f"/api/v1/uploads/{upload['id']}/complete").json() == result

    status = client.get(f"/api/v1/projects/{project['id']}/status").json()
    assert status["status"] == "uploaded"
    assert status["source_video_id"] == result["source_video_id"]
    metadata = client.get(f"/api/v1/projects/{project['id']}/source-video")
    assert metadata.status_code == 200
    download = client.get(f"/api/v1/assets/{result['asset_id']}/download-url")
    assert download.status_code == 200
    assert download.json()["url"].startswith("vidgen-file://")

    with factory() as session:
        source = session.get(SourceVideo, UUID(result["source_video_id"]))
        asset = session.get(Asset, UUID(result["asset_id"]))
        assert source is not None and asset is not None
        assert asset.project_id == source.project_id
        assert asset.generation_parameters["part_count"] == math.ceil(len(content) / part_size)


def test_new_upload_becomes_latest_without_breaking_prior_completion(
    api_client: tuple[TestClient, sessionmaker[Session]], golden_video: Path
) -> None:
    client, factory = api_client
    project = create_project(client)
    content = golden_video.read_bytes()
    completed: list[dict[str, Any]] = []

    for filename in ("first.mp4", "replacement.mp4"):
        upload = initialize(
            client,
            project["id"],
            content,
            filename=filename,
            part_size=max(4096, len(content)),
        )
        assert (
            client.put(f"/api/v1/uploads/{upload['id']}/parts/0", content=content).status_code
            == 200
        )
        response = client.post(f"/api/v1/uploads/{upload['id']}/complete")
        assert response.status_code == 200
        completed.append(response.json())

    metadata = client.get(f"/api/v1/projects/{project['id']}/source-video")
    status_response = client.get(f"/api/v1/projects/{project['id']}/status")
    assert metadata.json()["id"] == completed[1]["source_video_id"]
    assert status_response.json()["source_video_id"] == completed[1]["source_video_id"]

    first_retry = client.post(f"/api/v1/uploads/{completed[0]['upload_id']}/complete")
    assert first_retry.status_code == 200
    assert first_retry.json() == completed[0]
    with factory() as session:
        source_count = session.scalar(
            select(func.count())
            .select_from(SourceVideo)
            .where(SourceVideo.project_id == UUID(project["id"]))
        )
        assert source_count == 2


def test_upload_validation_and_finalization_recovery(
    api_client: tuple[TestClient, sessionmaker[Session]], golden_video: Path
) -> None:
    client, factory = api_client
    project = create_project(client)
    content = golden_video.read_bytes()
    unsupported = client.post(
        f"/api/v1/projects/{project['id']}/uploads",
        json={
            "filename": "bad.avi",
            "media_type": "video/x-msvideo",
            "expected_size": len(content),
            "expected_sha256": hashlib.sha256(content).hexdigest(),
            "part_size": 4096,
        },
    )
    assert unsupported.status_code == 422

    upload = initialize(
        client,
        project["id"],
        content,
        expected_sha256="0" * 64,
        part_size=max(4096, len(content)),
    )
    assert client.put(f"/api/v1/uploads/{upload['id']}/parts/0", content=content).status_code == 200
    mismatch = client.post(f"/api/v1/uploads/{upload['id']}/complete")
    assert mismatch.status_code == 422
    assert mismatch.json()["detail_code"] == "hash_mismatch"

    recovery_project = create_project(client)
    recovery = initialize(
        client,
        recovery_project["id"],
        content,
        part_size=max(4096, len(content)),
    )
    client.put(f"/api/v1/uploads/{recovery['id']}/parts/0", content=content)
    with factory() as session:
        row = session.get(UploadSession, UUID(recovery["id"]))
        assert row is not None
        row.status = "finalizing"
        session.commit()
    assert client.post(f"/api/v1/uploads/{recovery['id']}/complete").status_code == 200


def test_finalize_rejects_non_mp4_container(
    api_client: tuple[TestClient, sessionmaker[Session]],
) -> None:
    client, _ = api_client
    project = create_project(client)
    content = b"not actually a video" * 100
    upload = initialize(client, project["id"], content, part_size=4096)
    client.put(f"/api/v1/uploads/{upload['id']}/parts/0", content=content)
    response = client.post(f"/api/v1/uploads/{upload['id']}/complete")
    assert response.status_code == 422
    assert response.json()["detail_code"] == "invalid_video_container"


def test_project_and_assets_are_owner_scoped(
    api_client: tuple[TestClient, sessionmaker[Session]],
) -> None:
    client, _ = api_client
    project = create_project(client)
    assert (
        client.get(
            f"/api/v1/projects/{project['id']}", headers={"X-VidGen-User": "other-user"}
        ).status_code
        == 404
    )
    assert client.get("/api/v1/projects", headers={"X-VidGen-User": "other-user"}).json() == []
