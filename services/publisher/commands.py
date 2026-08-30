"""Composed T25 entry points for the CLI, the API and the Temporal activities.

Callers choose a provider and hand over a session; everything else - the
keyring, the OAuth service, eligibility, identity, restart safety and the
projections - is assembled here, so the three surfaces cannot drift apart.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from sqlalchemy.orm import Session

from services.publisher import youtube as capabilities
from services.publisher.contracts import YouTubeProvider
from services.publisher.credentials import Keyring, keyring_from_environment
from services.publisher.eligibility import PublicationEligibilityService
from services.publisher.fake_youtube import FakeYouTubeProvider, FakeYouTubeState, shared_state
from services.publisher.oauth import OAuthSettings, YouTubeOAuthService
from services.publisher.pipeline import PublicationOptions, PublicationPipeline
from services.publisher.projections import result_projection
from services.publisher.providers import (
    FAKE_PROVIDER,
    YOUTUBE_PROVIDER,
    PublisherConfigurationError,
    build_provider,
)
from vidgen.contracts.publication import PrivacyState, PublicationGate, PublicationResult
from vidgen.db.publication_repository import PublicationRepository
from vidgen.storage.blob import BlobStore


@dataclass(frozen=True, slots=True)
class PublisherCommandOptions:
    """Everything a caller may configure at the command boundary."""

    provider: str = FAKE_PROVIDER
    connection_id: UUID | None = None
    thumbnail_asset_id: UUID | None = None
    idempotency_key: str | None = None
    chunk_bytes: int = capabilities.DEFAULT_CHUNK_BYTES
    require_captions: bool = False
    require_thumbnail: bool = False
    max_processing_polls: int | None = None
    #: Chunks one upload activity sends before returning. Bounds the activity's
    #: duration without slowing the upload: every confirmed offset is durable,
    #: so the workflow simply re-enters and continues from there.
    max_chunks_per_drive: int | None = None
    trace_context: dict[str, str] = field(default_factory=dict)
    #: Shared state for the deterministic fake, so a test or a local run can
    #: inspect exactly which calls were made.
    fake_state: FakeYouTubeState | None = None


def keyring_from_settings(*, allow_development_key: bool | None = None) -> Keyring:
    """Build the credential keyring from the environment.

    The development fallback is opt-in and never silent: an unconfigured
    deployment fails with the missing Key Vault secret named, rather than
    sealing real refresh tokens with a key that is in this repository.
    """
    explicit = (
        allow_development_key
        if allow_development_key is not None
        else os.getenv("VIDGEN_YOUTUBE_ALLOW_DEV_ENCRYPTION_KEY", "false").lower() == "true"
    )
    return keyring_from_environment(
        key=os.getenv("VIDGEN_YOUTUBE_TOKEN_ENCRYPTION_KEY"),
        key_version=os.getenv("VIDGEN_YOUTUBE_TOKEN_ENCRYPTION_KEY_VERSION"),
        retired_keys=os.getenv("VIDGEN_YOUTUBE_TOKEN_ENCRYPTION_RETIRED_KEYS"),
        allow_development_key=explicit,
    )


def oauth_settings_from_environment() -> OAuthSettings:
    client_id = os.getenv("VIDGEN_YOUTUBE_OAUTH_CLIENT_ID", "")
    redirect_uri = os.getenv(
        "VIDGEN_YOUTUBE_OAUTH_REDIRECT_URI", "http://localhost:8000/api/v1/youtube/oauth:callback"
    )
    targets = tuple(
        item.strip()
        for item in os.getenv("VIDGEN_YOUTUBE_OAUTH_REDIRECT_TARGETS", "/").split(",")
        if item.strip()
    )
    return OAuthSettings(
        client_id=client_id or "local-development-client-id",
        redirect_uri=redirect_uri,
        allowed_redirect_targets=targets,
    )


def build_publisher_provider(options: PublisherCommandOptions) -> YouTubeProvider:
    if options.provider == FAKE_PROVIDER:
        return FakeYouTubeProvider(options.fake_state or shared_state())
    if options.provider != YOUTUBE_PROVIDER:
        raise PublisherConfigurationError(f"unsupported publication provider {options.provider!r}")
    return build_provider(
        YOUTUBE_PROVIDER,
        client_id=os.getenv("VIDGEN_YOUTUBE_OAUTH_CLIENT_ID"),
        client_secret=os.getenv("VIDGEN_YOUTUBE_OAUTH_CLIENT_SECRET"),
    )


def build_pipeline(
    session: Session,
    blob_store: BlobStore,
    options: PublisherCommandOptions,
    *,
    provider: YouTubeProvider | None = None,
    keyring: Keyring | None = None,
    oauth_settings: OAuthSettings | None = None,
) -> PublicationPipeline:
    """Assemble a pipeline with its provider, keyring and OAuth service."""
    resolved_provider = provider or build_publisher_provider(options)
    resolved_keyring = keyring or keyring_from_settings(
        allow_development_key=options.provider == FAKE_PROVIDER or None
    )
    repository = PublicationRepository(session, resolved_keyring)
    oauth = YouTubeOAuthService(
        repository, resolved_provider, oauth_settings or oauth_settings_from_environment()
    )
    return PublicationPipeline(
        session,
        blob_store,
        resolved_provider,
        keyring=resolved_keyring,
        oauth=oauth,
        options=PublicationOptions(
            chunk_bytes=options.chunk_bytes,
            max_processing_polls=options.max_processing_polls,
            max_chunks_per_drive=options.max_chunks_per_drive,
            require_captions=options.require_captions,
            require_thumbnail=options.require_thumbnail,
            trace_context=dict(options.trace_context),
        ),
    )


def evaluate_gate(
    session: Session,
    blob_store: BlobStore,
    *,
    project_id: UUID,
    owner_subject: str,
    connection_id: UUID | None,
    thumbnail_asset_id: UUID | None = None,
) -> PublicationGate:
    """The publication gate alone, with no side effects and no provider call."""
    gate, _ = PublicationEligibilityService(session, blob_store).evaluate(
        project_id=project_id,
        owner_subject=owner_subject,
        connection_id=connection_id,
        thumbnail_asset_id=thumbnail_asset_id,
    )
    return gate


async def publish_project(
    session: Session,
    blob_store: BlobStore,
    *,
    project_id: UUID,
    owner_subject: str,
    options: PublisherCommandOptions | None = None,
    pipeline: PublicationPipeline | None = None,
) -> PublicationResult:
    """Create or resume the publication for one project's current render."""
    resolved = options or PublisherCommandOptions()
    engine = pipeline or build_pipeline(session, blob_store, resolved)
    key = resolved.idempotency_key or f"publish:{project_id}"
    if resolved.connection_id is None:
        connections = engine.repository.connections_for_owner(owner_subject)
        connection_id = connections[0].id if connections else None
    else:
        connection_id = resolved.connection_id
    run = engine.create_draft(
        project_id=project_id,
        owner_subject=owner_subject,
        connection_id=connection_id,  # type: ignore[arg-type]
        idempotency_key=key,
        thumbnail_asset_id=resolved.thumbnail_asset_id,
    )
    session.commit()
    try:
        return await engine.start(run)
    finally:
        await _close(engine.provider)


async def resume_publication(
    session: Session,
    blob_store: BlobStore,
    *,
    publication_run_id: UUID,
    project_id: UUID,
    owner_subject: str,
    options: PublisherCommandOptions | None = None,
    pipeline: PublicationPipeline | None = None,
) -> PublicationResult:
    resolved = options or PublisherCommandOptions()
    engine = pipeline or build_pipeline(session, blob_store, resolved)
    run = engine.repository.owned_run(publication_run_id, project_id, owner_subject)
    if run is None:
        raise LookupError("the requested publication was not found")
    try:
        return await engine.resume(run)
    finally:
        await _close(engine.provider)


async def apply_visibility(
    session: Session,
    blob_store: BlobStore,
    *,
    publication_run_id: UUID,
    project_id: UUID,
    owner_subject: str,
    privacy: PrivacyState,
    scheduled_publish_at: datetime | None = None,
    notify_subscribers: bool = False,
    options: PublisherCommandOptions | None = None,
    pipeline: PublicationPipeline | None = None,
) -> PublicationResult:
    """Apply an explicit visibility decision. Always a deliberate user action."""
    resolved = options or PublisherCommandOptions()
    engine = pipeline or build_pipeline(session, blob_store, resolved)
    run = engine.repository.owned_run(publication_run_id, project_id, owner_subject)
    if run is None:
        raise LookupError("the requested publication was not found")
    try:
        return await engine.apply_visibility(
            run,
            privacy=privacy,
            actor=owner_subject,
            scheduled_publish_at=scheduled_publish_at,
            notify_subscribers=notify_subscribers,
        )
    finally:
        await _close(engine.provider)


def inspect_publication(
    session: Session, *, publication_run_id: UUID, project_id: UUID, owner_subject: str
) -> PublicationResult:
    """A read-only projection. Never contacts YouTube."""
    keyring = keyring_from_settings(allow_development_key=True)
    repository = PublicationRepository(session, keyring)
    run = repository.owned_run(publication_run_id, project_id, owner_subject)
    if run is None:
        raise LookupError("the requested publication was not found")
    return result_projection(session, run)


async def _close(provider: YouTubeProvider) -> None:
    close = getattr(provider, "aclose", None)
    if close is not None:
        await close()
