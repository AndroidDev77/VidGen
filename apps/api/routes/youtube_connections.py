"""Owner-scoped YouTube channel connections.

Four endpoints, and every one of them is careful about the same thing: the OAuth
credential never crosses this boundary. A connection projection carries the
channel, the granted scopes, the status and the *version* of the envelope key -
never a token, never ciphertext.

The callback validates the one-time ``state`` and the PKCE verifier stored with
it, not the development ``X-VidGen-User`` header. The header only says which
owner's rows to address, exactly as it does everywhere else; the state is what
proves this callback belongs to the flow that owner started. Until real
application authentication exists, this environment stays private - see
``infra/README.md``.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Response, status
from sqlalchemy.orm import Session

from apps.api.routes._common import (
    IdempotencyKeyDep,
    IfMatchDep,
    PrincipalDep,
    SessionDep,
    SettingsDep,
    idempotency_for,
)
from apps.api.schemas.publications import (
    DisconnectResponse,
    OAuthCallbackResponse,
    OAuthStartRequest,
    OAuthStartResponse,
    YouTubeChannelProjection,
    YouTubeConnectionCollection,
    YouTubeConnectionProjection,
)
from apps.api.settings import APISettings
from services.publisher.commands import PublisherCommandOptions, build_publisher_provider
from services.publisher.credentials import (
    CredentialCipherError,
    Keyring,
    development_keyring,
    keyring_from_environment,
)
from services.publisher.oauth import (
    OAuthConfigurationError,
    OAuthFlowError,
    OAuthSettings,
    YouTubeOAuthService,
)
from services.publisher.youtube import REQUIRED_SCOPES
from vidgen.contracts.review import ApiErrorCode
from vidgen.db.publication_models import YouTubeConnection
from vidgen.db.publication_repository import PublicationRepository
from vidgen.review.errors import conflict, not_found

router = APIRouter(prefix="/youtube", tags=["youtube-connections"])

START_OPERATION = "youtube:oauth-start"
DISCONNECT_OPERATION = "youtube:disconnect"


def _oauth_settings(settings: APISettings) -> OAuthSettings:
    try:
        return OAuthSettings(
            client_id=settings.youtube_oauth_client_id or "",
            redirect_uri=settings.youtube_oauth_redirect_uri,
            allowed_redirect_targets=tuple(settings.youtube_oauth_redirect_targets),
            scopes=REQUIRED_SCOPES,
        )
    except OAuthConfigurationError as error:
        raise conflict(ApiErrorCode.VALIDATION_FAILED, str(error)) from error


def keyring_for(settings: APISettings) -> Keyring:
    """The credential keyring, or a structured refusal naming what is missing."""
    try:
        return keyring_from_environment(
            key=settings.youtube_token_encryption_key,
            key_version=settings.youtube_token_encryption_key_version,
            retired_keys=settings.youtube_token_encryption_retired_keys,
            allow_development_key=settings.youtube_allow_dev_encryption_key,
        )
    except CredentialCipherError as error:
        # Names the missing configuration, never a key or a ciphertext.
        raise conflict(ApiErrorCode.VALIDATION_FAILED, str(error)) from error


def _service(session: Session, settings: APISettings) -> YouTubeOAuthService:
    keyring = keyring_for(settings)
    provider = build_publisher_provider(PublisherCommandOptions(provider=settings.youtube_provider))
    return YouTubeOAuthService(
        PublicationRepository(session, keyring), provider, _oauth_settings(settings)
    )


def _projection(connection: YouTubeConnection) -> YouTubeConnectionProjection:
    return YouTubeConnectionProjection(
        connection_id=connection.id,
        channel=YouTubeChannelProjection(
            channel_id=connection.channel_id,
            title=connection.channel_title or "",
            thumbnail_url=connection.channel_thumbnail_url or "",
            custom_url=connection.custom_url or "",
        ),
        status=connection.status,
        granted_scopes=list(connection.granted_scopes or []),
        encryption_key_version=connection.encryption_key_version or "",
        credential_expires_at=connection.credential_expires_at,
        last_verified_at=connection.last_verified_at,
        error_code=connection.error_code,
        created_at=connection.created_at,
        updated_at=connection.updated_at,
    )


@router.get("/connections", response_model=YouTubeConnectionCollection)
def list_connections(
    session: SessionDep, principal: PrincipalDep, settings: SettingsDep
) -> YouTubeConnectionCollection:
    keyring_available = bool(
        settings.youtube_token_encryption_key or settings.youtube_allow_dev_encryption_key
    )
    # Listing never opens a ciphertext, so an unconfigured envelope key must not
    # make the page fail: the projection has nothing sealed in it.
    repository = PublicationRepository(session, development_keyring())
    items = [_projection(row) for row in repository.connections_for_owner(principal.subject)]
    session.commit()
    return YouTubeConnectionCollection(
        items=items,
        oauth_configured=bool(settings.youtube_oauth_client_id) and keyring_available,
        # Stated rather than implied: the API still trusts a development
        # identity header, so this deployment is private by design.
        production_authentication_available=False,
    )


@router.post(
    "/oauth:start",
    response_model=OAuthStartResponse,
    status_code=status.HTTP_201_CREATED,
)
def start_oauth(
    request: OAuthStartRequest,
    session: SessionDep,
    principal: PrincipalDep,
    settings: SettingsDep,
    response: Response,
    if_match: IfMatchDep = None,
    idempotency_key: IdempotencyKeyDep = None,
) -> OAuthStartResponse:
    """Create a one-time state and return Google's authorization URL.

    The raw ``state`` exists only inside the returned URL: the database keeps
    its SHA-256, so reading the row cannot replay the callback.
    """
    idempotency = idempotency_for(session, principal)
    key = idempotency.require_key(START_OPERATION, idempotency_key)
    payload = request.model_dump(mode="json")
    replay = idempotency.replay(START_OPERATION, principal.subject, key, payload)
    if replay is not None:
        return OAuthStartResponse.model_validate(replay)
    service = _service(session, settings)
    try:
        authorization, _state = service.start(
            owner_subject=principal.subject, redirect_target=request.redirect_target
        )
    except OAuthFlowError as error:
        raise conflict(ApiErrorCode.VALIDATION_FAILED, str(error)) from error
    body = OAuthStartResponse(
        state_id=authorization.state_id,
        authorization_url=authorization.authorization_url,
        expires_at=authorization.expires_at,
        row_version=0,
    )
    idempotency.record(
        START_OPERATION,
        principal.subject,
        key,
        payload,
        status.HTTP_201_CREATED,
        body.model_dump(mode="json"),
    )
    session.commit()
    response.headers["Cache-Control"] = "no-store"
    return body


@router.get("/oauth:callback", response_model=OAuthCallbackResponse)
async def oauth_callback(
    session: SessionDep,
    principal: PrincipalDep,
    settings: SettingsDep,
    response: Response,
    code: Annotated[str, Query(min_length=1, max_length=2048)],
    state: Annotated[str, Query(min_length=1, max_length=512)],
) -> OAuthCallbackResponse:
    """Complete an authorization. The code is exchanged only here, on the backend."""
    service = _service(session, settings)
    try:
        connection, target = await service.complete(
            state=state, code=code, owner_subject=principal.subject
        )
    except OAuthFlowError as error:
        session.commit()
        raise conflict(ApiErrorCode.VALIDATION_FAILED, str(error)) from error
    session.commit()
    # The browser must never cache a callback: the URL contains an
    # authorization code, and a cached copy is a replayable credential.
    response.headers["Cache-Control"] = "no-store"
    return OAuthCallbackResponse(
        connection_id=connection.id,
        channel=YouTubeChannelProjection(
            channel_id=connection.channel_id,
            title=connection.channel_title or "",
            thumbnail_url=connection.channel_thumbnail_url or "",
            custom_url=connection.custom_url or "",
        ),
        status=connection.status,
        redirect_target=target,
    )


@router.delete("/connections/{connection_id}", response_model=DisconnectResponse)
async def disconnect(
    connection_id: UUID,
    session: SessionDep,
    principal: PrincipalDep,
    settings: SettingsDep,
    idempotency_key: IdempotencyKeyDep = None,
) -> DisconnectResponse:
    """Revoke at Google when possible, then forget the sealed credential."""
    idempotency = idempotency_for(session, principal)
    key = idempotency.require_key(DISCONNECT_OPERATION, idempotency_key)
    payload = {"connection_id": str(connection_id)}
    replay = idempotency.replay(DISCONNECT_OPERATION, str(connection_id), key, payload)
    if replay is not None:
        return DisconnectResponse.model_validate(replay)
    service = _service(session, settings)
    connection = service.repository.owned_connection(connection_id, principal.subject)
    if connection is None:
        # A foreign connection is indistinguishable from a missing one.
        raise not_found("YouTube connection")
    await service.disconnect(connection)
    body = DisconnectResponse(connection_id=connection.id, status=connection.status, revoked=True)
    idempotency.record(
        DISCONNECT_OPERATION,
        str(connection_id),
        key,
        payload,
        status.HTTP_200_OK,
        body.model_dump(mode="json"),
    )
    session.commit()
    return body


__all__ = ["router"]
