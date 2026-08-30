"""T25 API: owner scoping, optimistic concurrency, idempotency and redaction.

The control plane never uploads anything and never returns a credential. These
tests assert both: the shapes the dashboard consumes, and the things that must
never appear in them.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from services.publisher import youtube as capabilities
from services.publisher.credentials import development_keyring
from tests.publication_fixtures import build_publishable_project, connect_fake_channel
from vidgen.db.publication_models import PublicationRun, YouTubeOAuthState
from vidgen.db.publication_repository import PublicationRepository
from vidgen.review.workflow_control import FakeWorkflowController, publication_workflow_id
from vidgen.storage.blob import FilesystemBlobStore

HEADERS = {"X-VidGen-User": "local-user"}


def _store(client: TestClient) -> FilesystemBlobStore:
    from apps.api.dependencies import get_blob_store

    override = client.app.dependency_overrides[get_blob_store]
    store = override()
    assert isinstance(store, FilesystemBlobStore)
    return store


def _row_version(client: TestClient, project_id: str) -> int:
    response = client.get(f"/api/v1/projects/{project_id}/publications", headers=HEADERS)
    assert response.status_code == 200
    return int(response.json()["gate"]["row_version"])


# -- connections ---------------------------------------------------------------
def test_listing_connections_is_owner_scoped_and_free_of_credentials(
    publication_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
) -> None:
    client, factory, _ = publication_client
    with factory() as session:
        connect_fake_channel(session, owner_subject="local-user")
        connect_fake_channel(session, owner_subject="someone-else")
    response = client.get("/api/v1/youtube/connections", headers=HEADERS)
    assert response.status_code == 200
    payload = response.json()
    assert len(payload["items"]) == 1
    assert payload["oauth_configured"] is True
    # Stated honestly rather than implied.
    assert payload["production_authentication_available"] is False
    serialized = json.dumps(payload)
    for forbidden in ("fake-refresh-token", "fake-access-token", "refresh_token", "ciphertext"):
        assert forbidden not in serialized
    assert payload["items"][0]["encryption_key_version"]


def test_starting_an_oauth_flow_returns_a_url_with_no_secret_in_it(
    publication_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
) -> None:
    client, factory, _ = publication_client
    response = client.post(
        "/api/v1/youtube/oauth:start",
        json={"redirect_target": "/projects"},
        headers={**HEADERS, "Idempotency-Key": "oauth-1"},
    )
    assert response.status_code == 201
    payload = response.json()
    url = payload["authorization_url"]
    assert url.startswith(capabilities.OAUTH_AUTHORIZATION_URL)
    assert "code_challenge_method=S256" in url
    assert "client_secret" not in url
    assert response.headers["Cache-Control"] == "no-store"
    with factory() as session:
        rows = session.query(YouTubeOAuthState).all()
        assert len(rows) == 1
        assert len(rows[0].state_hash) == 64
        # The verifier is sealed, never stored in the clear.
        assert len(rows[0].code_verifier_nonce) == 12

    # Repeating the same key replays rather than creating a second state.
    again = client.post(
        "/api/v1/youtube/oauth:start",
        json={"redirect_target": "/projects"},
        headers={**HEADERS, "Idempotency-Key": "oauth-1"},
    )
    assert again.json()["state_id"] == payload["state_id"]
    with factory() as session:
        assert session.query(YouTubeOAuthState).count() == 1


def test_an_oauth_start_without_an_idempotency_key_is_refused(
    publication_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
) -> None:
    client, _, _ = publication_client
    response = client.post(
        "/api/v1/youtube/oauth:start", json={"redirect_target": ""}, headers=HEADERS
    )
    assert response.status_code == 428


def test_a_non_allowlisted_redirect_target_is_refused(
    publication_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
) -> None:
    client, _, _ = publication_client
    response = client.post(
        "/api/v1/youtube/oauth:start",
        json={"redirect_target": "https://evil.example/"},
        headers={**HEADERS, "Idempotency-Key": "oauth-evil"},
    )
    assert response.status_code == 409


def test_the_callback_validates_state_rather_than_the_identity_header(
    publication_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
) -> None:
    client, _, _ = publication_client
    started = client.post(
        "/api/v1/youtube/oauth:start",
        json={"redirect_target": "/projects"},
        headers={**HEADERS, "Idempotency-Key": "oauth-1"},
    )
    from urllib.parse import parse_qs, urlparse

    state = parse_qs(urlparse(started.json()["authorization_url"]).query)["state"][0]

    # A fabricated state is refused even with a valid identity header.
    forged = client.get(
        "/api/v1/youtube/oauth:callback",
        params={"code": "c", "state": "not-the-state"},
        headers=HEADERS,
    )
    assert forged.status_code == 409

    completed = client.get(
        "/api/v1/youtube/oauth:callback",
        params={"code": "authorization-code", "state": state},
        headers=HEADERS,
    )
    assert completed.status_code == 200
    assert completed.headers["Cache-Control"] == "no-store"
    payload = completed.json()
    assert payload["channel"]["channel_id"]
    assert payload["redirect_target"] == "/projects"
    assert "token" not in json.dumps(payload).lower()

    # The state is single use.
    replayed = client.get(
        "/api/v1/youtube/oauth:callback",
        params={"code": "authorization-code", "state": state},
        headers=HEADERS,
    )
    assert replayed.status_code == 409


def test_disconnecting_a_foreign_connection_is_a_404(
    publication_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
) -> None:
    client, factory, _ = publication_client
    with factory() as session:
        foreign, _, _ = connect_fake_channel(session, owner_subject="someone-else")
        foreign_id = foreign.id
    response = client.request(
        "DELETE",
        f"/api/v1/youtube/connections/{foreign_id}",
        headers={**HEADERS, "Idempotency-Key": "disconnect-1"},
    )
    assert response.status_code == 404


# -- publications --------------------------------------------------------------
def test_the_gate_explains_why_a_project_cannot_publish(
    publication_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
) -> None:
    client, factory, _ = publication_client
    store = _store(client)
    with factory() as session:
        fixture = build_publishable_project(session, store, approved=False)
    response = client.get(f"/api/v1/projects/{fixture.project_id}/publications", headers=HEADERS)
    assert response.status_code == 200
    gate = response.json()["gate"]
    assert gate["allowed"] is False
    assert any(failure["code"] == "RENDER_NOT_APPROVED" for failure in gate["failures"])


def test_creating_a_publication_requires_if_match_and_an_idempotency_key(
    publication_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
) -> None:
    client, factory, _ = publication_client
    store = _store(client)
    with factory() as session:
        fixture = build_publishable_project(session, store)
        connection, _, _ = connect_fake_channel(session)
        connection_id = str(connection.id)
    body = {"connection_id": connection_id, "thumbnail_asset_id": str(fixture.thumbnail_asset_id)}
    url = f"/api/v1/projects/{fixture.project_id}/publications"

    assert client.post(url, json=body, headers=HEADERS).status_code == 428
    missing_match = client.post(url, json=body, headers={**HEADERS, "Idempotency-Key": "pub-1"})
    assert missing_match.status_code == 409

    version = _row_version(client, str(fixture.project_id))
    created = client.post(
        url,
        json=body,
        headers={**HEADERS, "Idempotency-Key": "pub-1", "If-Match": str(version)},
    )
    assert created.status_code == 201
    payload = created.json()
    assert payload["status"] == "DRAFT"
    assert payload["requested_privacy"] == "private"
    assert payload["contains_synthetic_media"] is True
    assert payload["notify_subscribers"] is False
    assert payload["metadata"]["title"]
    assert created.headers["ETag"] == f'"{version}"'

    # A stale If-Match loses.
    stale = client.post(
        url,
        json={**body, "thumbnail_asset_id": None},
        headers={**HEADERS, "Idempotency-Key": "pub-2", "If-Match": str(version - 1)},
    )
    assert stale.status_code == 409

    # Replaying the same key returns the same publication.
    replayed = client.post(
        url,
        json=body,
        headers={**HEADERS, "Idempotency-Key": "pub-1", "If-Match": str(version)},
    )
    assert replayed.json()["publication_id"] == payload["publication_id"]
    with factory() as session:
        assert session.query(PublicationRun).count() == 1


def test_reusing_an_idempotency_key_with_a_different_body_is_a_conflict(
    publication_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
) -> None:
    client, factory, _ = publication_client
    store = _store(client)
    with factory() as session:
        fixture = build_publishable_project(session, store)
        connection, _, _ = connect_fake_channel(session)
        connection_id = str(connection.id)
    url = f"/api/v1/projects/{fixture.project_id}/publications"
    version = _row_version(client, str(fixture.project_id))
    headers = {**HEADERS, "Idempotency-Key": "pub-1", "If-Match": str(version)}
    assert (
        client.post(url, json={"connection_id": connection_id}, headers=headers).status_code == 201
    )
    conflicting = client.post(
        url,
        json={
            "connection_id": connection_id,
            "thumbnail_asset_id": str(fixture.thumbnail_asset_id),
        },
        headers=headers,
    )
    assert conflicting.status_code == 409


def test_starting_a_publication_hands_the_workflow_ids_only(
    publication_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
) -> None:
    client, factory, controller = publication_client
    store = _store(client)
    with factory() as session:
        fixture = build_publishable_project(session, store)
        connection, _, _ = connect_fake_channel(session)
        connection_id = str(connection.id)
    version = _row_version(client, str(fixture.project_id))
    created = client.post(
        f"/api/v1/projects/{fixture.project_id}/publications",
        json={"connection_id": connection_id},
        headers={**HEADERS, "Idempotency-Key": "pub-1", "If-Match": str(version)},
    ).json()
    started = client.post(
        f"/api/v1/projects/{fixture.project_id}/publications/{created['publication_id']}:start",
        json={"resume": False},
        headers={**HEADERS, "Idempotency-Key": "start-1", "If-Match": str(version)},
    )
    assert started.status_code == 202
    workflow_id = publication_workflow_id(created["publication_id"])
    assert workflow_id in controller.publications
    message = controller.publications[workflow_id]
    # Compact and ID-only: no metadata text, no credential, no session URI.
    payload = message.model_dump(mode="json")
    assert set(payload) == {
        "schema_version",
        "project_id",
        "publication_run_id",
        "connection_id",
        "final_render_asset_id",
        "idempotency_key",
        "trace_context",
    }
    serialized = json.dumps(payload)
    for forbidden in ("title", "token", "upload", "http", "caption"):
        assert forbidden not in serialized.lower()

    # A repeated start adopts the same workflow rather than creating a second.
    client.post(
        f"/api/v1/projects/{fixture.project_id}/publications/{created['publication_id']}:start",
        json={"resume": False},
        headers={**HEADERS, "Idempotency-Key": "start-2", "If-Match": str(version)},
    )
    assert len(controller.publications) == 1


def test_a_cross_owner_publication_is_a_404(
    publication_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
) -> None:
    client, factory, _ = publication_client
    store = _store(client)
    with factory() as session:
        fixture = build_publishable_project(session, store, owner_subject="owner-a")
        connection, _, _ = connect_fake_channel(session, owner_subject="owner-a")
        connection_id = str(connection.id)
    owner_a = {"X-VidGen-User": "owner-a"}
    version = int(
        client.get(f"/api/v1/projects/{fixture.project_id}/publications", headers=owner_a).json()[
            "gate"
        ]["row_version"]
    )
    created = client.post(
        f"/api/v1/projects/{fixture.project_id}/publications",
        json={"connection_id": connection_id},
        headers={**owner_a, "Idempotency-Key": "pub-1", "If-Match": str(version)},
    ).json()
    stolen = client.get(
        f"/api/v1/projects/{fixture.project_id}/publications/{created['publication_id']}",
        headers={"X-VidGen-User": "owner-b"},
    )
    assert stolen.status_code == 404


def test_an_unknown_publication_is_a_404(
    publication_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
) -> None:
    client, factory, _ = publication_client
    store = _store(client)
    with factory() as session:
        fixture = build_publishable_project(session, store)
    response = client.get(
        f"/api/v1/projects/{fixture.project_id}/publications/{uuid4()}", headers=HEADERS
    )
    assert response.status_code == 404


def test_the_detail_projection_never_returns_a_session_uri_or_a_token(
    publication_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
) -> None:
    client, factory, _ = publication_client
    store = _store(client)
    with factory() as session:
        fixture = build_publishable_project(session, store)
        connection, state, _ = connect_fake_channel(session)
        connection_id = str(connection.id)
    version = _row_version(client, str(fixture.project_id))
    created = client.post(
        f"/api/v1/projects/{fixture.project_id}/publications",
        json={
            "connection_id": connection_id,
            "thumbnail_asset_id": str(fixture.thumbnail_asset_id),
        },
        headers={**HEADERS, "Idempotency-Key": "pub-1", "If-Match": str(version)},
    ).json()

    # Run the pipeline out of band, as the publisher worker would.
    import asyncio

    from services.publisher.fake_youtube import FakeYouTubeProvider
    from services.publisher.oauth import YouTubeOAuthService
    from services.publisher.pipeline import PublicationOptions, PublicationPipeline
    from services.publisher.processing import ProcessingPoller
    from tests.publication_fixtures import OAUTH_SETTINGS

    async def instant(seconds: float) -> None:
        return None

    with factory() as session:
        provider = FakeYouTubeProvider(state)
        repository = PublicationRepository(session, development_keyring())
        pipeline = PublicationPipeline(
            session,
            store,
            provider,
            keyring=development_keyring(),
            oauth=YouTubeOAuthService(repository, provider, OAUTH_SETTINGS),
            options=PublicationOptions(
                chunk_bytes=capabilities.RESUMABLE_CHUNK_GRANULARITY,
                max_processing_polls=6,
            ),
            poller=ProcessingPoller(provider, initial_seconds=0.0, max_seconds=0.0, sleep=instant),
        )
        run = session.get(PublicationRun, UUID(created["publication_id"]))
        assert run is not None
        asyncio.run(pipeline.start(run))

    detail = client.get(
        f"/api/v1/projects/{fixture.project_id}/publications/{created['publication_id']}",
        headers=HEADERS,
    )
    assert detail.status_code == 200
    payload = detail.json()
    assert payload["status"] == "PRIVATE_READY"
    assert payload["actual_privacy"] == "private"
    assert payload["video_id"]
    assert payload["video_url"].startswith("https://www.youtube.com/watch?v=")
    assert payload["confirmed_offset"] == payload["total_bytes"] > 0
    assert payload["caption_status"] == "succeeded"
    assert payload["thumbnail_status"] == "succeeded"
    assert payload["quota_units"] > 0
    assert payload["attempts"]
    serialized = json.dumps(payload)
    for forbidden in (
        "fake-upload.googleapis.com",
        "fake-access-token",
        "fake-refresh-token",
        "session_uri",
        "authorization_code",
    ):
        assert forbidden not in serialized


def test_visibility_requires_an_explicit_request_and_reports_what_youtube_returned(
    publication_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
) -> None:
    client, factory, _ = publication_client
    store = _store(client)
    import asyncio

    from services.publisher.fake_youtube import FakeYouTubeProvider
    from services.publisher.oauth import YouTubeOAuthService
    from services.publisher.pipeline import PublicationOptions, PublicationPipeline
    from services.publisher.processing import ProcessingPoller
    from tests.publication_fixtures import OAUTH_SETTINGS

    async def instant(seconds: float) -> None:
        return None

    with factory() as session:
        fixture = build_publishable_project(session, store)
        connection, state, _ = connect_fake_channel(session)
        connection_id = str(connection.id)
    version = _row_version(client, str(fixture.project_id))
    created = client.post(
        f"/api/v1/projects/{fixture.project_id}/publications",
        json={"connection_id": connection_id},
        headers={**HEADERS, "Idempotency-Key": "pub-1", "If-Match": str(version)},
    ).json()
    with factory() as session:
        provider = FakeYouTubeProvider(state)
        repository = PublicationRepository(session, development_keyring())
        pipeline = PublicationPipeline(
            session,
            store,
            provider,
            keyring=development_keyring(),
            oauth=YouTubeOAuthService(repository, provider, OAUTH_SETTINGS),
            options=PublicationOptions(
                chunk_bytes=capabilities.RESUMABLE_CHUNK_GRANULARITY,
                max_processing_polls=6,
            ),
            poller=ProcessingPoller(provider, initial_seconds=0.0, max_seconds=0.0, sleep=instant),
        )
        run = session.get(PublicationRun, UUID(created["publication_id"]))
        assert run is not None
        asyncio.run(pipeline.start(run))

    response = client.post(
        f"/api/v1/projects/{fixture.project_id}/publications/"
        f"{created['publication_id']}:visibility",
        json={"privacy": "unlisted", "scheduled_publish_at": None, "notify_subscribers": False},
        headers={**HEADERS, "Idempotency-Key": "vis-1", "If-Match": str(version)},
    )
    assert response.status_code == 200, response.json().get("summary")
    payload = response.json()
    assert payload["status"] == "PUBLISHED"
    assert payload["actual_privacy"] == "unlisted"
    assert payload["notify_subscribers"] is False


def test_cancelling_a_draft_is_allowed_and_cancelling_a_published_video_is_not(
    publication_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
) -> None:
    client, factory, _ = publication_client
    store = _store(client)
    with factory() as session:
        fixture = build_publishable_project(session, store)
        connection, _, _ = connect_fake_channel(session)
        connection_id = str(connection.id)
    version = _row_version(client, str(fixture.project_id))
    created = client.post(
        f"/api/v1/projects/{fixture.project_id}/publications",
        json={"connection_id": connection_id},
        headers={**HEADERS, "Idempotency-Key": "pub-1", "If-Match": str(version)},
    ).json()
    cancelled = client.post(
        f"/api/v1/projects/{fixture.project_id}/publications/{created['publication_id']}:cancel",
        json={"reason": "changed my mind"},
        headers={**HEADERS, "Idempotency-Key": "cancel-1", "If-Match": str(version)},
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "CANCELLED"

    restarted = client.post(
        f"/api/v1/projects/{fixture.project_id}/publications/{created['publication_id']}:start",
        json={"resume": False},
        headers={**HEADERS, "Idempotency-Key": "start-1", "If-Match": str(version)},
    )
    assert restarted.status_code == 409


def test_the_openapi_document_exposes_the_publication_surface(
    publication_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
) -> None:
    client, _, _ = publication_client
    paths = client.get("/openapi.json").json()["paths"]
    for path in (
        "/api/v1/youtube/connections",
        "/api/v1/youtube/oauth:start",
        "/api/v1/youtube/oauth:callback",
        "/api/v1/youtube/connections/{connection_id}",
        "/api/v1/projects/{project_id}/publications",
        "/api/v1/projects/{project_id}/publications/{publication_id}",
        "/api/v1/projects/{project_id}/publications/{publication_id}:start",
        "/api/v1/projects/{project_id}/publications/{publication_id}:cancel",
        "/api/v1/projects/{project_id}/publications/{publication_id}:resume",
        "/api/v1/projects/{project_id}/publications/{publication_id}:visibility",
    ):
        assert path in paths, path


def test_the_api_settings_offer_nowhere_to_leak_a_token(tmp_path: Path) -> None:
    from apps.api.settings import APISettings

    fields = set(APISettings.model_fields)
    assert "youtube_oauth_client_secret" in fields
    assert "youtube_token_encryption_key" in fields
    # There is no YouTube refresh- or access-token setting: those only ever
    # exist sealed in the database, never in configuration.
    youtube_fields = {name for name in fields if name.startswith("youtube_")}
    assert not any("refresh_token" in name or "access_token" in name for name in youtube_fields)


# -- draft editing -------------------------------------------------------------
def _create_publication(
    client: TestClient, factory: sessionmaker[Session], key: str = "pub-1"
) -> tuple[dict[str, object], UUID]:
    """Build an eligible project, connect a channel and create its draft."""
    store = _store(client)
    with factory() as session:
        fixture = build_publishable_project(session, store)
        connection, _, _ = connect_fake_channel(session)
        connection_id = str(connection.id)
    version = _row_version(client, str(fixture.project_id))
    created = client.post(
        f"/api/v1/projects/{fixture.project_id}/publications",
        json={
            "connection_id": connection_id,
            "thumbnail_asset_id": str(fixture.thumbnail_asset_id),
        },
        headers={**HEADERS, "Idempotency-Key": key, "If-Match": str(version)},
    )
    assert created.status_code == 201, created.text
    return created.json(), fixture.project_id


def test_editing_a_draft_versions_the_publication_that_already_exists(
    publication_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
) -> None:
    client, factory, _ = publication_client
    created, project_id = _create_publication(client, factory)
    publication_id = created["publication_id"]
    url = f"/api/v1/projects/{project_id}/publications/{publication_id}"
    body = {**created["metadata"], "title": "A better title"}
    body.pop("metadata_version", None)

    version = _row_version(client, str(project_id))
    edited = client.patch(
        url,
        json=body,
        headers={**HEADERS, "Idempotency-Key": "draft-1", "If-Match": str(version)},
    )
    assert edited.status_code == 200, edited.text
    payload = edited.json()
    assert payload["publication_id"] == publication_id
    assert payload["metadata"]["title"] == "A better title"
    assert payload["metadata_version"] == int(created["metadata_version"]) + 1

    # Editing must never mint a second publication for the same render.
    with factory() as session:
        assert session.query(PublicationRun).count() == 1


def test_editing_a_draft_requires_if_match_and_an_idempotency_key(
    publication_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
) -> None:
    client, factory, _ = publication_client
    created, project_id = _create_publication(client, factory)
    url = f"/api/v1/projects/{project_id}/publications/{created['publication_id']}"
    body = {**created["metadata"], "title": "Retitled"}
    body.pop("metadata_version", None)

    assert client.patch(url, json=body, headers=HEADERS).status_code == 428
    missing_match = client.patch(url, json=body, headers={**HEADERS, "Idempotency-Key": "d-1"})
    assert missing_match.status_code == 409

    version = _row_version(client, str(project_id))
    stale = client.patch(
        url,
        json=body,
        headers={**HEADERS, "Idempotency-Key": "d-2", "If-Match": str(version - 1)},
    )
    assert stale.status_code == 409


def test_replaying_a_draft_edit_key_returns_the_first_answer(
    publication_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
) -> None:
    client, factory, _ = publication_client
    created, project_id = _create_publication(client, factory)
    url = f"/api/v1/projects/{project_id}/publications/{created['publication_id']}"
    body = {**created["metadata"], "title": "Once only"}
    body.pop("metadata_version", None)

    version = _row_version(client, str(project_id))
    headers = {**HEADERS, "Idempotency-Key": "draft-replay", "If-Match": str(version)}
    first = client.patch(url, json=body, headers=headers)
    assert first.status_code == 200
    replayed = client.patch(url, json=body, headers=headers)
    assert replayed.status_code == 200
    assert replayed.json() == first.json()

    # The same key with a different draft is a conflict, not a silent replay.
    conflicting = client.patch(
        url,
        json={**body, "title": "Something else"},
        headers=headers,
    )
    assert conflicting.status_code == 409


def test_a_schedule_on_a_non_public_draft_is_a_readable_conflict(
    publication_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
) -> None:
    """A cross-field contract rule reaches the caller as a message, not a 500."""
    client, factory, _ = publication_client
    created, project_id = _create_publication(client, factory)
    url = f"/api/v1/projects/{project_id}/publications/{created['publication_id']}"
    body = {**created["metadata"]}
    body.pop("metadata_version", None)
    body["requested_privacy"] = "private"
    body["scheduled_publish_at"] = "2030-01-01T00:00:00Z"

    version = _row_version(client, str(project_id))
    refused = client.patch(
        url,
        json=body,
        headers={**HEADERS, "Idempotency-Key": "draft-bad", "If-Match": str(version)},
    )
    assert refused.status_code == 409
    detail = json.dumps(refused.json())
    assert "Traceback" not in detail
    assert "scheduled" in detail.lower() or "public" in detail.lower()


def test_creating_a_publication_with_an_impossible_schedule_is_a_conflict(
    publication_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
) -> None:
    client, factory, _ = publication_client
    store = _store(client)
    with factory() as session:
        fixture = build_publishable_project(session, store)
        connection, _, _ = connect_fake_channel(session)
        connection_id = str(connection.id)
    version = _row_version(client, str(fixture.project_id))
    refused = client.post(
        f"/api/v1/projects/{fixture.project_id}/publications",
        json={
            "connection_id": connection_id,
            "metadata": {
                "title": "Scheduled but private",
                "requested_privacy": "private",
                "scheduled_publish_at": "2030-01-01T00:00:00Z",
            },
        },
        headers={**HEADERS, "Idempotency-Key": "pub-bad", "If-Match": str(version)},
    )
    assert refused.status_code == 409
    assert "Traceback" not in json.dumps(refused.json())
    with factory() as session:
        assert session.query(PublicationRun).count() == 0


def test_a_cross_owner_draft_edit_is_a_404(
    publication_client: tuple[TestClient, sessionmaker[Session], FakeWorkflowController],
) -> None:
    client, factory, _ = publication_client
    created, project_id = _create_publication(client, factory)
    url = f"/api/v1/projects/{project_id}/publications/{created['publication_id']}"
    body = {**created["metadata"], "title": "Not yours"}
    body.pop("metadata_version", None)
    version = _row_version(client, str(project_id))
    foreign = client.patch(
        url,
        json=body,
        headers={
            "X-VidGen-User": "owner-b",
            "Idempotency-Key": "draft-foreign",
            "If-Match": str(version),
        },
    )
    assert foreign.status_code == 404
