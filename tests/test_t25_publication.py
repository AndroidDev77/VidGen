"""T25: eligibility, OAuth, credential storage, identity and metadata.

Everything here is offline and deterministic. The only YouTube implementation
these tests touch is the in-process fake, and the production adapter is
exercised separately against a mocked transport in
``tests/test_youtube_adapter.py``. No test in this repository makes a real
YouTube request.
"""

from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import vidgen.db  # noqa: F401 - completes Base.metadata
from services.publisher import youtube as capabilities
from services.publisher.credentials import (
    CredentialCipherError,
    Keyring,
    SecretValue,
    development_keyring,
    generate_key,
    keyring_from_environment,
)
from services.publisher.eligibility import PublicationEligibilityService
from services.publisher.fake_youtube import FakeYouTubeProvider, FakeYouTubeState
from services.publisher.metadata import (
    PublicationMetadataError,
    initial_draft,
    metadata_hash,
    publication_identity,
    to_provider_metadata,
    validate,
)
from services.publisher.oauth import (
    OAuthConfigurationError,
    OAuthFlowError,
    OAuthSettings,
    YouTubeOAuthService,
    code_challenge_for,
    generate_code_verifier,
    generate_state,
    validate_redirect_target,
)
from services.publisher.pipeline import PublicationPipeline
from services.publisher.providers import build_provider
from tests.publication_fixtures import (
    OAUTH_SETTINGS,
    build_publishable_project,
    connect_fake_channel,
)
from vidgen.contracts.publication import (
    ConnectionStatus,
    PrivacyState,
    PublicationFailureCode,
    PublicationStatus,
    transition_allowed,
)
from vidgen.db.base import Base
from vidgen.db.models import RenderJob
from vidgen.db.publication_repository import (
    PublicationRepository,
    PublicationStateError,
    state_hash,
)
from vidgen.db.review_models import DownstreamInvalidation, RenderApproval
from vidgen.storage.blob import FilesystemBlobStore


@pytest.fixture
def factory(tmp_path: Path) -> sessionmaker[Session]:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'publication.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def store(tmp_path: Path) -> FilesystemBlobStore:
    return FilesystemBlobStore(tmp_path / "blobs", b"test-secret")


# -- eligibility ---------------------------------------------------------------
def test_an_approved_current_passing_render_is_publishable(
    factory: sessionmaker[Session], store: FilesystemBlobStore
) -> None:
    with factory() as session:
        fixture = build_publishable_project(session, store)
        connection, _, _ = connect_fake_channel(session)
        gate, render = PublicationEligibilityService(session, store).evaluate(
            project_id=fixture.project_id,
            owner_subject=fixture.owner_subject,
            connection_id=connection.id,
            thumbnail_asset_id=fixture.thumbnail_asset_id,
        )
    assert gate.allowed, gate.failures
    assert render is not None
    assert gate.final_render_asset_id == fixture.final_asset_id
    assert gate.approval_id == fixture.approval_id
    assert gate.completion_gate_id == fixture.completion_gate_id
    assert gate.caption_asset_id == fixture.caption_asset_id


def test_a_render_without_a_t18_approval_is_refused(
    factory: sessionmaker[Session], store: FilesystemBlobStore
) -> None:
    with factory() as session:
        fixture = build_publishable_project(session, store, approved=False)
        connection, _, _ = connect_fake_channel(session)
        gate, render = PublicationEligibilityService(session, store).evaluate(
            project_id=fixture.project_id,
            owner_subject=fixture.owner_subject,
            connection_id=connection.id,
        )
    assert not gate.allowed and render is None
    assert PublicationFailureCode.RENDER_NOT_APPROVED in {f.code for f in gate.failures}


def test_a_render_without_a_current_t22_pass_is_refused(
    factory: sessionmaker[Session], store: FilesystemBlobStore
) -> None:
    with factory() as session:
        fixture = build_publishable_project(session, store, gate_decision="FAIL")
        connection, _, _ = connect_fake_channel(session)
        gate, _ = PublicationEligibilityService(session, store).evaluate(
            project_id=fixture.project_id,
            owner_subject=fixture.owner_subject,
            connection_id=connection.id,
        )
    assert not gate.allowed
    assert PublicationFailureCode.COMPLETION_GATE_NOT_PASSED in {f.code for f in gate.failures}


def test_a_missing_completion_gate_row_is_refused(
    factory: sessionmaker[Session], store: FilesystemBlobStore
) -> None:
    with factory() as session:
        fixture = build_publishable_project(session, store, with_gate=False)
        connection, _, _ = connect_fake_channel(session)
        gate, _ = PublicationEligibilityService(session, store).evaluate(
            project_id=fixture.project_id,
            owner_subject=fixture.owner_subject,
            connection_id=connection.id,
        )
    assert not gate.allowed
    assert PublicationFailureCode.COMPLETION_GATE_NOT_PASSED in {f.code for f in gate.failures}


def test_a_render_invalidated_after_rendering_is_stale(
    factory: sessionmaker[Session], store: FilesystemBlobStore
) -> None:
    with factory() as session:
        fixture = build_publishable_project(session, store)
        connection, _, _ = connect_fake_channel(session)
        session.add(
            DownstreamInvalidation(
                project_id=fixture.project_id,
                origin_type="script",
                origin_id=fixture.graph.script_id,
                invalidated_type="render",
                invalidated_id=fixture.render_job_id,
                reason="script_edited",
            )
        )
        session.commit()
        gate, _ = PublicationEligibilityService(session, store).evaluate(
            project_id=fixture.project_id,
            owner_subject=fixture.owner_subject,
            connection_id=connection.id,
        )
    assert not gate.allowed
    assert PublicationFailureCode.STALE_RENDER in {f.code for f in gate.failures}


def test_an_unselected_render_is_refused(
    factory: sessionmaker[Session], store: FilesystemBlobStore
) -> None:
    with factory() as session:
        fixture = build_publishable_project(session, store)
        connection, _, _ = connect_fake_channel(session)
        job = session.get(RenderJob, fixture.render_job_id)
        assert job is not None
        job.selected = False
        session.commit()
        gate, _ = PublicationEligibilityService(session, store).evaluate(
            project_id=fixture.project_id,
            owner_subject=fixture.owner_subject,
            connection_id=connection.id,
        )
    assert not gate.allowed
    assert PublicationFailureCode.RENDER_NOT_SELECTED in {f.code for f in gate.failures}


def test_another_owners_project_is_indistinguishable_from_a_missing_one(
    factory: sessionmaker[Session], store: FilesystemBlobStore
) -> None:
    with factory() as session:
        fixture = build_publishable_project(session, store, owner_subject="owner-a")
        connection, _, _ = connect_fake_channel(session, owner_subject="owner-b")
        gate, _ = PublicationEligibilityService(session, store).evaluate(
            project_id=fixture.project_id,
            owner_subject="owner-b",
            connection_id=connection.id,
        )
    assert not gate.allowed
    assert gate.failures[0].code is PublicationFailureCode.CROSS_PROJECT_REFERENCE
    assert "not found" in gate.failures[0].summary.lower()


def test_a_connection_owned_by_another_account_is_refused(
    factory: sessionmaker[Session], store: FilesystemBlobStore
) -> None:
    with factory() as session:
        fixture = build_publishable_project(session, store, owner_subject="owner-a")
        foreign, _, _ = connect_fake_channel(session, owner_subject="owner-b")
        gate, _ = PublicationEligibilityService(session, store).evaluate(
            project_id=fixture.project_id,
            owner_subject="owner-a",
            connection_id=foreign.id,
        )
    assert not gate.allowed
    assert PublicationFailureCode.CONNECTION_NOT_OWNED in {f.code for f in gate.failures}


def test_a_cross_project_thumbnail_is_refused(
    factory: sessionmaker[Session], store: FilesystemBlobStore
) -> None:
    with factory() as session:
        first = build_publishable_project(session, store, name="first")
        second = build_publishable_project(session, store, name="second")
        connection, _, _ = connect_fake_channel(session)
        gate, _ = PublicationEligibilityService(session, store).evaluate(
            project_id=first.project_id,
            owner_subject=first.owner_subject,
            connection_id=connection.id,
            thumbnail_asset_id=second.thumbnail_asset_id,
        )
    assert not gate.allowed
    assert PublicationFailureCode.INVALID_THUMBNAIL_ASSET in {f.code for f in gate.failures}


def test_an_unreadable_final_asset_is_refused(
    factory: sessionmaker[Session], store: FilesystemBlobStore, tmp_path: Path
) -> None:
    with factory() as session:
        fixture = build_publishable_project(session, store)
        connection, _, _ = connect_fake_channel(session)
        from vidgen.db.models import Asset

        asset = session.get(Asset, fixture.final_asset_id)
        assert asset is not None
        (Path(store.root) / asset.storage_key).unlink()
        gate, _ = PublicationEligibilityService(session, store).evaluate(
            project_id=fixture.project_id,
            owner_subject=fixture.owner_subject,
            connection_id=connection.id,
        )
    assert not gate.allowed
    assert PublicationFailureCode.MISSING_FINAL_ASSET in {f.code for f in gate.failures}


def test_a_revoked_approval_does_not_count(
    factory: sessionmaker[Session], store: FilesystemBlobStore
) -> None:
    with factory() as session:
        fixture = build_publishable_project(session, store)
        connection, _, _ = connect_fake_channel(session)
        approval = session.get(RenderApproval, fixture.approval_id)
        assert approval is not None
        approval.revoked_at = datetime.now(UTC)
        session.commit()
        gate, _ = PublicationEligibilityService(session, store).evaluate(
            project_id=fixture.project_id,
            owner_subject=fixture.owner_subject,
            connection_id=connection.id,
        )
    assert not gate.allowed
    assert PublicationFailureCode.RENDER_NOT_APPROVED in {f.code for f in gate.failures}


# -- OAuth ---------------------------------------------------------------------
def test_state_is_random_hashed_and_never_stored_in_the_clear(
    factory: sessionmaker[Session],
) -> None:
    with factory() as session:
        repository = PublicationRepository(session, development_keyring())
        service = YouTubeOAuthService(
            repository, FakeYouTubeProvider(FakeYouTubeState()), OAUTH_SETTINGS
        )
        first, raw_first = service.start(owner_subject="local-user")
        second, raw_second = service.start(owner_subject="local-user")
        session.commit()
        assert raw_first != raw_second
        from vidgen.db.publication_models import YouTubeOAuthState

        rows = session.query(YouTubeOAuthState).all()
        stored = {row.state_hash for row in rows}
        assert state_hash(raw_first) in stored
        # The raw value appears nowhere in the row.
        assert all(raw_first not in (row.redirect_uri + row.redirect_target) for row in rows)
    assert first.authorization_url.startswith(capabilities.OAUTH_AUTHORIZATION_URL)
    assert "code_challenge_method=S256" in second.authorization_url
    assert "access_type=offline" in second.authorization_url


def test_pkce_challenge_is_the_s256_of_the_verifier() -> None:
    verifier = generate_code_verifier()
    assert (
        capabilities.PKCE_VERIFIER_MIN_LENGTH
        <= len(verifier)
        <= capabilities.PKCE_VERIFIER_MAX_LENGTH
    )
    import hashlib

    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    assert code_challenge_for(verifier) == expected


def test_a_state_can_be_consumed_exactly_once(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        repository = PublicationRepository(session, development_keyring())
        service = YouTubeOAuthService(
            repository, FakeYouTubeProvider(FakeYouTubeState()), OAUTH_SETTINGS
        )
        _, raw = service.start(owner_subject="local-user")
        session.commit()
        asyncio.run(service.complete(state=raw, code="c", owner_subject="local-user"))
        session.commit()
        with pytest.raises(OAuthFlowError) as error:
            asyncio.run(service.complete(state=raw, code="c", owner_subject="local-user"))
    assert "already been used" in str(error.value)


def test_an_expired_state_is_refused(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        repository = PublicationRepository(session, development_keyring())
        service = YouTubeOAuthService(
            repository, FakeYouTubeProvider(FakeYouTubeState()), OAUTH_SETTINGS
        )
        _, raw = service.start(owner_subject="local-user")
        session.commit()
        later = datetime.now(UTC) + timedelta(seconds=capabilities.OAUTH_STATE_TTL_SECONDS + 1)
        with pytest.raises(OAuthFlowError) as error:
            asyncio.run(
                service.complete(state=raw, code="c", owner_subject="local-user", now=later)
            )
    assert "expired" in str(error.value)


def test_an_unknown_state_is_refused(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        repository = PublicationRepository(session, development_keyring())
        service = YouTubeOAuthService(
            repository, FakeYouTubeProvider(FakeYouTubeState()), OAUTH_SETTINGS
        )
        with pytest.raises(OAuthFlowError):
            asyncio.run(
                service.complete(state=generate_state(), code="c", owner_subject="local-user")
            )


def test_a_callback_from_a_different_owner_is_refused(
    factory: sessionmaker[Session],
) -> None:
    """The one-time state, not the development identity header, is the authority."""
    with factory() as session:
        repository = PublicationRepository(session, development_keyring())
        service = YouTubeOAuthService(
            repository, FakeYouTubeProvider(FakeYouTubeState()), OAUTH_SETTINGS
        )
        _, raw = service.start(owner_subject="owner-a")
        session.commit()
        with pytest.raises(OAuthFlowError) as error:
            asyncio.run(service.complete(state=raw, code="c", owner_subject="owner-b"))
    assert error.value.code is PublicationFailureCode.CONNECTION_NOT_OWNED


def test_the_redirect_allowlist_refuses_an_external_target() -> None:
    assert validate_redirect_target("/projects", OAUTH_SETTINGS) == "/projects"
    assert validate_redirect_target("", OAUTH_SETTINGS) == ""
    for hostile in ("https://evil.example/", "//evil.example/", "/../etc"):
        with pytest.raises(OAuthFlowError):
            validate_redirect_target(hostile, OAUTH_SETTINGS)


def test_partner_and_cms_scopes_are_never_configurable() -> None:
    with pytest.raises(OAuthConfigurationError, match="Partner"):
        OAuthSettings(
            client_id="x",
            redirect_uri="https://example.test/cb",
            scopes=("https://www.googleapis.com/auth/youtubepartner",),
        )


def test_a_plaintext_redirect_is_only_allowed_on_loopback() -> None:
    OAuthSettings(client_id="x", redirect_uri="http://localhost:8000/cb")
    with pytest.raises(OAuthConfigurationError, match=r"loopback|localhost"):
        OAuthSettings(client_id="x", redirect_uri="http://staging.example/cb")


def test_the_required_scope_set_covers_captions_and_thumbnails() -> None:
    """``youtube.upload`` alone cannot insert a caption track."""
    assert capabilities.SCOPE_FORCE_SSL in capabilities.REQUIRED_SCOPES
    assert capabilities.OPERATION_SCOPES["captions.insert"] == (capabilities.SCOPE_FORCE_SSL,)
    assert capabilities.SCOPE_UPLOAD not in capabilities.OPERATION_SCOPES["captions.insert"]
    assert capabilities.missing_scopes((capabilities.SCOPE_UPLOAD,), "captions.insert") == (
        capabilities.SCOPE_FORCE_SSL,
    )
    for scope in capabilities.REQUIRED_SCOPES:
        for forbidden in capabilities.FORBIDDEN_SCOPE_FRAGMENTS:
            assert forbidden not in scope


def test_the_channel_identity_comes_from_youtube_not_the_browser(
    factory: sessionmaker[Session],
) -> None:
    with factory() as session:
        state = FakeYouTubeState(channel_id="UCserverauthoritative01")
        connection, _, _ = connect_fake_channel(session, state=state)
    assert connection.channel_id == "UCserverauthoritative01"


def test_a_partial_scope_grant_is_refused(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        state = FakeYouTubeState(restricted_scopes=(capabilities.SCOPE_UPLOAD,))
        repository = PublicationRepository(session, development_keyring())
        service = YouTubeOAuthService(repository, FakeYouTubeProvider(state), OAUTH_SETTINGS)
        _, raw = service.start(owner_subject="local-user")
        session.commit()
        with pytest.raises(OAuthFlowError) as error:
            asyncio.run(service.complete(state=raw, code="c", owner_subject="local-user"))
    assert error.value.code is PublicationFailureCode.INSUFFICIENT_SCOPE


def test_an_invalid_grant_moves_the_connection_to_reauthorization(
    factory: sessionmaker[Session],
) -> None:
    with factory() as session:
        connection, state, keyring = connect_fake_channel(session)
        state.invalid_grant = True
        # Expire the cached access token so a refresh is actually attempted.
        repository = PublicationRepository(session, keyring)
        repository.store_access_token(
            connection, SecretValue("stale"), datetime.now(UTC) - timedelta(hours=1)
        )
        session.commit()
        service = YouTubeOAuthService(repository, FakeYouTubeProvider(state), OAUTH_SETTINGS)
        with pytest.raises(OAuthFlowError) as error:
            asyncio.run(service.access_token_for(connection))
        session.commit()
        assert error.value.code is PublicationFailureCode.INVALID_GRANT
        assert connection.status == ConnectionStatus.REAUTHORIZATION_REQUIRED.value


def test_a_cached_access_token_is_reused_until_it_nears_expiry(
    factory: sessionmaker[Session],
) -> None:
    with factory() as session:
        connection, state, keyring = connect_fake_channel(session)
        repository = PublicationRepository(session, keyring)
        service = YouTubeOAuthService(repository, FakeYouTubeProvider(state), OAUTH_SETTINGS)
        repository.store_access_token(
            connection, SecretValue("fresh"), datetime.now(UTC) + timedelta(hours=1)
        )
        session.commit()
        before = state.count("oauth.refresh")
        token = asyncio.run(service.access_token_for(connection))
        assert token.reveal() == "fresh"
        assert state.count("oauth.refresh") == before


def test_disconnecting_deletes_the_sealed_credential(
    factory: sessionmaker[Session],
) -> None:
    with factory() as session:
        connection, state, keyring = connect_fake_channel(session)
        repository = PublicationRepository(session, keyring)
        service = YouTubeOAuthService(repository, FakeYouTubeProvider(state), OAUTH_SETTINGS)
        asyncio.run(service.disconnect(connection))
        session.commit()
        from vidgen.db.publication_models import YouTubeConnectionSecret

        assert session.query(YouTubeConnectionSecret).count() == 0
        assert connection.credential_present is False
        assert connection.status == ConnectionStatus.REVOKED.value
        assert state.count("oauth.revoke") == 1


def test_a_channel_that_moved_forces_reauthorization(
    factory: sessionmaker[Session],
) -> None:
    with factory() as session:
        connection, state, keyring = connect_fake_channel(session)
        state.channel_id = "UCsomeotherchannel99"
        repository = PublicationRepository(session, keyring)
        service = YouTubeOAuthService(repository, FakeYouTubeProvider(state), OAUTH_SETTINGS)
        with pytest.raises(OAuthFlowError) as error:
            asyncio.run(service.verify_channel(connection))
        session.commit()
    assert error.value.code is PublicationFailureCode.CHANNEL_MISMATCH


# -- credential storage --------------------------------------------------------
def test_a_refresh_token_is_never_stored_in_the_clear(
    factory: sessionmaker[Session],
) -> None:
    with factory() as session:
        connection, _, keyring = connect_fake_channel(session)
        from vidgen.db.publication_models import YouTubeConnectionSecret

        secret = session.query(YouTubeConnectionSecret).filter_by(connection_id=connection.id).one()
        assert b"fake-refresh-token" not in secret.refresh_token_ciphertext
        assert len(secret.refresh_token_nonce) == 12
        opened = PublicationRepository(session, keyring).refresh_token_for(connection)
        assert opened.reveal() == "fake-refresh-token"


def test_a_secret_value_does_not_print_itself() -> None:
    value = SecretValue("super-secret-token")
    assert "super-secret" not in repr(value)
    assert "super-secret" not in str(value)
    assert "super-secret" not in f"{value}"
    assert value.reveal() == "super-secret-token"


def test_ciphertext_is_bound_to_its_connection() -> None:
    keyring = development_keyring()
    sealed = keyring.seal("token", purpose="youtube.refresh_token", context="connection:a")
    with pytest.raises(CredentialCipherError, match="authentication"):
        keyring.open(sealed, context="connection:b")


def test_key_rotation_re_seals_without_losing_the_credential(
    factory: sessionmaker[Session],
) -> None:
    old = Keyring({"v1": bytes(range(32))}, "v1")
    with factory() as session:
        connection, _, _ = connect_fake_channel(session, keyring=old)
        assert connection.encryption_key_version == "v1"
        new = old.with_key("v2", bytes(reversed(range(32))))
        repository = PublicationRepository(session, new)
        assert repository.rotate_connection_keys(connection) is True
        session.commit()
        assert connection.encryption_key_version == "v2"
        assert repository.refresh_token_for(connection).reveal() == "fake-refresh-token"
        # Rotating again is a no-op rather than a re-encryption.
        assert repository.rotate_connection_keys(connection) is False


def test_a_rotated_out_key_is_reported_by_name_not_guessed() -> None:
    keyring = Keyring({"v2": bytes(range(32))}, "v2")
    sealed = Keyring({"v1": bytes(reversed(range(32)))}, "v1").seal(
        "t", purpose="youtube.refresh_token", context="connection:a"
    )
    with pytest.raises(CredentialCipherError, match="v1"):
        keyring.open(sealed, context="connection:a")


def test_an_unconfigured_deployment_refuses_to_use_the_development_key() -> None:
    with pytest.raises(CredentialCipherError, match="VIDGEN_YOUTUBE_TOKEN_ENCRYPTION_KEY"):
        keyring_from_environment(key=None, key_version=None)
    development = keyring_from_environment(key=None, key_version=None, allow_development_key=True)
    assert development.is_development_only


def test_a_configured_key_must_name_its_version() -> None:
    with pytest.raises(CredentialCipherError, match="KEY_VERSION"):
        keyring_from_environment(key=generate_key(), key_version=None)


def test_a_short_key_is_refused() -> None:
    short = base64.urlsafe_b64encode(b"too-short").decode()
    with pytest.raises(CredentialCipherError, match="32 bytes"):
        keyring_from_environment(key=short, key_version="v1")


# -- identity and metadata -----------------------------------------------------
def test_the_publication_identity_binds_every_material_input(
    factory: sessionmaker[Session], store: FilesystemBlobStore
) -> None:
    with factory() as session:
        fixture = build_publishable_project(session, store)
        connection, _, _ = connect_fake_channel(session)
        _, render = PublicationEligibilityService(session, store).evaluate(
            project_id=fixture.project_id,
            owner_subject=fixture.owner_subject,
            connection_id=connection.id,
            thumbnail_asset_id=fixture.thumbnail_asset_id,
        )
        assert render is not None
        draft = initial_draft(render)
        base = dict(
            project_id=render.project.id,
            final_render_asset_id=render.final_asset.id,
            final_render_sha256=render.final_asset.sha256,
            final_editorial_run_id=render.final_editorial_run.id,
            final_report_hash=render.final_editorial_run.final_qa_identity,
            approval_id=render.approval.id,
            connection_id=render.connection.id,
            channel_id=render.connection.channel_id,
            metadata_version=draft.metadata_version,
            metadata_digest=metadata_hash(draft),
            caption_asset_id=render.caption_asset.id,
            caption_sha256=render.caption_asset.sha256,
            thumbnail_asset_id=render.thumbnail_asset.id if render.thumbnail_asset else None,
            thumbnail_sha256=render.thumbnail_asset.sha256 if render.thumbnail_asset else None,
        )
        identity = publication_identity(**base)
        assert identity == publication_identity(**base)
        for field, value in (
            ("final_render_sha256", "0" * 64),
            ("metadata_version", 2),
            ("channel_id", "UCdifferent"),
            ("caption_sha256", "1" * 64),
            ("thumbnail_sha256", "2" * 64),
        ):
            assert publication_identity(**{**base, field: value}) != identity
        assert publication_identity(**base, initial_privacy=PrivacyState.PRIVATE) == identity


def test_the_initial_draft_is_deterministic_and_uses_no_paid_model(
    factory: sessionmaker[Session], store: FilesystemBlobStore
) -> None:
    with factory() as session:
        fixture = build_publishable_project(session, store)
        connection, _, _ = connect_fake_channel(session)
        _, render = PublicationEligibilityService(session, store).evaluate(
            project_id=fixture.project_id,
            owner_subject=fixture.owner_subject,
            connection_id=connection.id,
        )
        assert render is not None
        first = initial_draft(render)
        second = initial_draft(render)
    assert first == second
    assert fixture.graph is not None
    assert "Season 3 Episode 4" in first.title
    assert first.contains_synthetic_media is True
    assert first.notify_subscribers is False
    assert first.requested_privacy is PrivacyState.PRIVATE
    assert first.initial_privacy is PrivacyState.PRIVATE


def test_metadata_limits_come_from_the_capability_registry() -> None:
    draft = initial_draft.__wrapped__ if hasattr(initial_draft, "__wrapped__") else None
    assert draft is None  # the draft builder is a plain function
    from vidgen.contracts.publication import PublicationMetadata

    with pytest.raises(ValueError):
        PublicationMetadata(title="x" * (capabilities.MAX_TITLE_LENGTH + 1))
    with pytest.raises(ValueError):
        PublicationMetadata(title="ok", description="<script>")
    with pytest.raises(ValueError):
        PublicationMetadata(title="ok", tags=["x" * 31])
    with pytest.raises(ValueError):
        PublicationMetadata(title="ok", tags=["y" * 30] * 30)


def test_a_scheduled_publication_must_be_a_valid_future_utc_instant() -> None:
    from vidgen.contracts.publication import PublicationMetadata

    soon = datetime.now(UTC) + timedelta(minutes=1)
    with pytest.raises(PublicationMetadataError, match="minutes in the future"):
        validate(
            PublicationMetadata(
                title="ok", requested_privacy=PrivacyState.PUBLIC, scheduled_publish_at=soon
            )
        )
    with pytest.raises(ValueError, match="UTC offset"):
        PublicationMetadata(
            title="ok",
            requested_privacy=PrivacyState.PUBLIC,
            scheduled_publish_at=datetime(2030, 1, 1),
        )
    with pytest.raises(ValueError, match="public privacy state"):
        PublicationMetadata(
            title="ok",
            requested_privacy=PrivacyState.UNLISTED,
            scheduled_publish_at=datetime.now(UTC) + timedelta(days=1),
        )
    validate(
        PublicationMetadata(
            title="ok",
            requested_privacy=PrivacyState.PUBLIC,
            scheduled_publish_at=datetime.now(UTC) + timedelta(days=1),
        )
    )


def test_a_public_initial_upload_cannot_be_expressed() -> None:
    from vidgen.contracts.publication import PublicationMetadata

    metadata = PublicationMetadata(title="ok", requested_privacy=PrivacyState.PUBLIC)
    provider_metadata = to_provider_metadata(metadata)
    assert provider_metadata.privacy_status == capabilities.PrivacyStatus.PRIVATE.value
    assert provider_metadata.notify_subscribers is False
    assert provider_metadata.contains_synthetic_media is True
    with pytest.raises(ValueError):
        PublicationMetadata(title="ok", initial_privacy=PrivacyState.PUBLIC)


def test_editing_a_draft_versions_it_only_when_it_really_changed(
    factory: sessionmaker[Session], store: FilesystemBlobStore
) -> None:
    with factory() as session:
        fixture = build_publishable_project(session, store)
        connection, state, keyring = connect_fake_channel(session)
        pipeline = _pipeline(session, store, state, keyring)
        run = pipeline.create_draft(
            project_id=fixture.project_id,
            owner_subject=fixture.owner_subject,
            connection_id=connection.id,
            idempotency_key="publish:1",
        )
        session.commit()
        draft = pipeline.draft_of(run)
        assert run.metadata_version == 1
        pipeline.update_draft(run, draft)
        assert run.metadata_version == 1
        pipeline.update_draft(run, draft.model_copy(update={"title": "Edited title"}))
        session.commit()
        assert run.metadata_version == 2
        assert pipeline.draft_of(run).title == "Edited title"
        # A resume must never regenerate over the edit.
        again = pipeline.create_draft(
            project_id=fixture.project_id,
            owner_subject=fixture.owner_subject,
            connection_id=connection.id,
            idempotency_key="publish:1",
        )
        assert pipeline.draft_of(again).title == "Edited title"


def test_the_state_machine_forbids_a_backwards_transition() -> None:
    assert transition_allowed(PublicationStatus.READY, PublicationStatus.UPLOAD_INITIALIZING)
    assert transition_allowed(PublicationStatus.UPLOADING, PublicationStatus.PROCESSING)
    # A caption failure never returns to an upload state.
    assert not transition_allowed(PublicationStatus.UPLOADING_CAPTIONS, PublicationStatus.UPLOADING)
    assert not transition_allowed(
        PublicationStatus.UPLOADING_THUMBNAIL, PublicationStatus.UPLOAD_INITIALIZING
    )
    assert not transition_allowed(PublicationStatus.PRIVATE_READY, PublicationStatus.UPLOADING)
    assert not transition_allowed(PublicationStatus.FAILED, PublicationStatus.READY)
    assert not transition_allowed(PublicationStatus.CANCELLED, PublicationStatus.READY)


def test_the_repository_refuses_an_illegal_transition(
    factory: sessionmaker[Session], store: FilesystemBlobStore
) -> None:
    with factory() as session:
        fixture = build_publishable_project(session, store)
        connection, state, keyring = connect_fake_channel(session)
        pipeline = _pipeline(session, store, state, keyring)
        run = pipeline.create_draft(
            project_id=fixture.project_id,
            owner_subject=fixture.owner_subject,
            connection_id=connection.id,
            idempotency_key="publish:1",
        )
        session.commit()
        with pytest.raises(PublicationStateError, match="may not move"):
            pipeline.repository.transition(run, PublicationStatus.PUBLISHED)


def test_recording_a_second_video_id_is_refused(
    factory: sessionmaker[Session], store: FilesystemBlobStore
) -> None:
    with factory() as session:
        fixture = build_publishable_project(session, store)
        connection, state, keyring = connect_fake_channel(session)
        pipeline = _pipeline(session, store, state, keyring)
        run = pipeline.create_draft(
            project_id=fixture.project_id,
            owner_subject=fixture.owner_subject,
            connection_id=connection.id,
            idempotency_key="publish:1",
        )
        pipeline.repository.record_video_id(run, "vid-first")
        with pytest.raises(PublicationStateError, match="already created"):
            pipeline.repository.record_video_id(run, "vid-second")


def test_the_provider_factory_rejects_an_unknown_provider() -> None:
    from services.publisher.providers import PublisherConfigurationError

    with pytest.raises(PublisherConfigurationError):
        build_provider("vimeo")
    with pytest.raises(PublisherConfigurationError, match="CLIENT_ID"):
        build_provider("youtube")


def _pipeline(
    session: Session,
    store: FilesystemBlobStore,
    state: FakeYouTubeState,
    keyring: Keyring,
) -> PublicationPipeline:
    provider = FakeYouTubeProvider(state)
    repository = PublicationRepository(session, keyring)
    return PublicationPipeline(
        session,
        store,
        provider,
        keyring=keyring,
        oauth=YouTubeOAuthService(repository, provider, OAUTH_SETTINGS),
    )
