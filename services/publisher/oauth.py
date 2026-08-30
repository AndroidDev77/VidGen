"""OAuth 2.0 connection of one YouTube channel to one VidGen owner.

The flow is the standard web-server flow with PKCE, and every hardening measure
it needs is enforced here rather than assumed:

* the redirect URI is the exact configured string, sent identically in the
  authorization request and the token exchange;
* ``state`` is 256 bits from :mod:`secrets`, stored only as a SHA-256 hash,
  bound to the owner who started the flow, expiring, and consumable exactly
  once;
* the PKCE verifier is sealed at rest and bound by AAD to its own state row;
* the post-login redirect target is checked against an allowlist both when it is
  stored and again when it is used, so widening the allowlist later cannot bless
  an old row;
* the authorization *code* is only ever exchanged on the backend;
* the channel identity is resolved from YouTube after the exchange - a channel
  ID supplied by the browser is never trusted;
* an ``invalid_grant`` moves the connection to ``REAUTHORIZATION_REQUIRED``
  rather than retrying against a credential the user has revoked.

The callback deliberately does **not** trust the development ``X-VidGen-User``
identity header for authorization: the one-time state is what proves this
callback belongs to the flow this owner started. The header is only used to
address the owner's own rows, exactly as everywhere else in the API, and the
production-authentication limitation is documented in ``infra/README.md``.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

from services.publisher import youtube as capabilities
from services.publisher.contracts import YouTubeProvider, YouTubeProviderError
from services.publisher.credentials import SecretValue
from vidgen.contracts.publication import (
    ConnectionStatus,
    OAuthAuthorizationRequest,
    PublicationFailureCode,
    YouTubeChannel,
)
from vidgen.contracts.publication import YouTubeConnection as YouTubeConnectionContract
from vidgen.db.publication_models import YouTubeConnection
from vidgen.db.publication_repository import PublicationRepository, PublicationStateError


class OAuthConfigurationError(RuntimeError):
    """The OAuth flow cannot start because configuration is missing or unsafe."""


class OAuthFlowError(RuntimeError):
    """A rejected authorization attempt. The message is safe to render."""

    def __init__(self, code: PublicationFailureCode, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class OAuthSettings:
    """The configuration one authorization flow needs.

    The client ID is ordinary configuration and appears in the browser URL. The
    client secret never leaves the backend, which is why it is not on this
    object: it is held by the provider adapter alone.
    """

    client_id: str
    #: The exact URI registered with Google. Byte-identical in both requests.
    redirect_uri: str
    #: Absolute or relative post-login targets the callback may send a browser
    #: to. Anything else is refused; an empty allowlist means "no redirect".
    allowed_redirect_targets: tuple[str, ...] = ()
    scopes: tuple[str, ...] = capabilities.REQUIRED_SCOPES
    state_ttl_seconds: int = capabilities.OAUTH_STATE_TTL_SECONDS

    def __post_init__(self) -> None:
        if not self.client_id.strip():
            raise OAuthConfigurationError("VIDGEN_YOUTUBE_OAUTH_CLIENT_ID is not configured")
        parsed = urlparse(self.redirect_uri)
        if parsed.scheme not in {"https", "http"} or not parsed.netloc:
            raise OAuthConfigurationError(
                "VIDGEN_YOUTUBE_OAUTH_REDIRECT_URI must be an absolute http(s) URI "
                "matching the one registered with Google"
            )
        if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "[::1]"}:
            # Google only permits plaintext redirects to loopback. Refusing it
            # here means a misconfigured staging URI fails at start-up rather
            # than at the callback, after the user has already consented.
            raise OAuthConfigurationError(
                "a plaintext OAuth redirect URI is only permitted for localhost"
            )
        for scope in self.scopes:
            for forbidden in capabilities.FORBIDDEN_SCOPE_FRAGMENTS:
                if forbidden in scope:
                    raise OAuthConfigurationError(
                        f"the scope {scope!r} is a YouTube Partner or CMS scope "
                        "and is never requested"
                    )

    @property
    def is_loopback(self) -> bool:
        return urlparse(self.redirect_uri).hostname in {"localhost", "127.0.0.1", "[::1]"}


def generate_state() -> str:
    """256 bits of URL-safe randomness. One authorization attempt, one value."""
    return secrets.token_urlsafe(32)


def generate_code_verifier() -> str:
    """A PKCE verifier inside RFC 7636's 43-128 character range."""
    verifier = secrets.token_urlsafe(64)
    return verifier[: capabilities.PKCE_VERIFIER_MAX_LENGTH]


def code_challenge_for(verifier: str) -> str:
    """The S256 challenge. ``plain`` is never used."""
    if not (
        capabilities.PKCE_VERIFIER_MIN_LENGTH
        <= len(verifier)
        <= capabilities.PKCE_VERIFIER_MAX_LENGTH
    ):
        raise OAuthFlowError(
            PublicationFailureCode.AUTHENTICATION_REQUIRED,
            "the PKCE verifier is outside the permitted length range",
        )
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def validate_redirect_target(target: str, settings: OAuthSettings) -> str:
    """Return ``target`` when the allowlist permits it, else raise.

    A relative path is only accepted when it is same-site by construction: it
    must start with a single ``/`` and must not start with ``//``, which a
    browser would read as a protocol-relative absolute URL.
    """
    candidate = (target or "").strip()
    if not candidate:
        return ""
    # A traversal segment or a backslash can escape a prefix match once the
    # browser normalises the path, so neither is ever accepted.
    if ".." in candidate or "\\" in candidate:
        raise OAuthFlowError(
            PublicationFailureCode.AUTHENTICATION_REQUIRED,
            "the requested post-authorization redirect target is not allowlisted",
        )
    if candidate in settings.allowed_redirect_targets:
        return candidate
    if candidate.startswith("/") and not candidate.startswith("//"):
        for allowed in settings.allowed_redirect_targets:
            if allowed.startswith("/") and candidate.startswith(allowed):
                return candidate
    raise OAuthFlowError(
        PublicationFailureCode.AUTHENTICATION_REQUIRED,
        "the requested post-authorization redirect target is not allowlisted",
    )


class YouTubeOAuthService:
    """Starts, completes and maintains one owner's YouTube connections."""

    def __init__(
        self,
        repository: PublicationRepository,
        provider: YouTubeProvider,
        settings: OAuthSettings,
    ) -> None:
        self.repository = repository
        self.provider = provider
        self.settings = settings

    # -- start ---------------------------------------------------------------
    def start(
        self, *, owner_subject: str, redirect_target: str = "", now: datetime | None = None
    ) -> tuple[OAuthAuthorizationRequest, str]:
        """Create a one-time state and return the URL to send the browser to.

        Returns the request and the raw ``state`` value. Only the hash is
        persisted, so this is the single moment the raw value exists; the caller
        puts it in the URL and nowhere else.
        """
        moment = now or datetime.now(UTC)
        target = validate_redirect_target(redirect_target, self.settings)
        state = generate_state()
        verifier = generate_code_verifier()
        challenge = code_challenge_for(verifier)
        row = self.repository.create_oauth_state(
            state=state,
            owner_subject=owner_subject,
            code_verifier=SecretValue(verifier),
            redirect_uri=self.settings.redirect_uri,
            redirect_target=target,
            requested_scopes=self.settings.scopes,
            expires_at=moment + timedelta(seconds=self.settings.state_ttl_seconds),
        )
        url = self.provider.authorization_url(
            redirect_uri=self.settings.redirect_uri,
            scopes=self.settings.scopes,
            state=state,
            code_challenge=challenge,
        )
        return (
            OAuthAuthorizationRequest(
                state_id=row.id, authorization_url=url, expires_at=row.expires_at
            ),
            state,
        )

    # -- callback ------------------------------------------------------------
    async def complete(
        self,
        *,
        state: str,
        code: str,
        owner_subject: str | None = None,
        now: datetime | None = None,
    ) -> tuple[YouTubeConnection, str]:
        """Exchange the code and persist the connection. Returns it and the target.

        ``owner_subject`` is checked against the state row when supplied; the
        state itself is the authority, so a callback carrying a mismatched
        development identity header is refused rather than silently rebinding
        the connection to whoever's browser arrived.
        """
        moment = now or datetime.now(UTC)
        try:
            row = self.repository.consume_oauth_state(state, now=moment)
        except PublicationStateError as error:
            raise OAuthFlowError(
                PublicationFailureCode.AUTHENTICATION_REQUIRED, str(error)
            ) from error
        if owner_subject is not None and row.owner_subject != owner_subject:
            raise OAuthFlowError(
                PublicationFailureCode.CONNECTION_NOT_OWNED,
                "this authorization request was started by a different account",
            )
        # Re-validated against the *current* allowlist: a target that was
        # allowlisted when the flow started but is not now must not be used.
        target = validate_redirect_target(row.redirect_target, self.settings)
        verifier = self.repository.code_verifier_for(row)
        try:
            tokens = await self.provider.exchange_code(
                code=SecretValue(code),
                redirect_uri=row.redirect_uri,
                code_verifier=verifier,
            )
        except YouTubeProviderError as error:
            raise OAuthFlowError(error.code, str(error)) from error

        granted = tuple(tokens.granted_scopes)
        missing = [scope for scope in self.settings.scopes if scope not in granted]
        if missing and granted:
            # Google returns the scopes actually granted. A partial grant would
            # fail later at captions.insert; refusing now keeps the user in the
            # consent screen where they can fix it.
            raise OAuthFlowError(
                PublicationFailureCode.INSUFFICIENT_SCOPE,
                "the authorization did not grant every permission publishing requires: "
                + ", ".join(scope.rsplit("/", 1)[-1] for scope in missing),
            )
        if tokens.refresh_token is None:
            raise OAuthFlowError(
                PublicationFailureCode.AUTHENTICATION_REQUIRED,
                "the authorization returned no offline refresh credential; reconnect and "
                "approve the consent screen again",
            )
        try:
            channel = await self.provider.fetch_channel(access_token=tokens.access_token)
        except YouTubeProviderError as error:
            raise OAuthFlowError(error.code, str(error)) from error
        if not channel.channel_id:
            raise OAuthFlowError(
                PublicationFailureCode.CHANNEL_MISMATCH,
                "the authorized Google account owns no YouTube channel",
            )
        connection = self.repository.upsert_connection(
            owner_subject=row.owner_subject,
            channel_id=channel.channel_id,
            channel_title=channel.title,
            channel_thumbnail_url=channel.thumbnail_url,
            custom_url=channel.custom_url,
            granted_scopes=granted or self.settings.scopes,
            refresh_token=tokens.refresh_token,
            access_token=tokens.access_token,
            access_token_expires_at=tokens.expires_at,
        )
        return connection, target

    # -- maintenance ---------------------------------------------------------
    async def access_token_for(
        self, connection: YouTubeConnection, *, now: datetime | None = None
    ) -> SecretValue:
        """A valid access token, refreshed from the sealed offline credential.

        A cached token is reused until it is within the refresh skew of expiry,
        so a long upload does not spend a request per chunk renewing it.
        """
        moment = now or datetime.now(UTC)
        if connection.status == ConnectionStatus.REVOKED.value:
            raise OAuthFlowError(
                PublicationFailureCode.INVALID_GRANT,
                "this YouTube connection was revoked; reconnect the channel",
            )
        cached = self.repository.cached_access_token(connection)
        if cached is not None:
            token, expires = cached
            skew = timedelta(seconds=capabilities.ACCESS_TOKEN_REFRESH_SKEW_SECONDS)
            if expires - skew > moment:
                return token
        refresh = self.repository.refresh_token_for(connection)
        try:
            tokens = await self.provider.refresh_access_token(refresh_token=refresh)
        except YouTubeProviderError as error:
            if error.code is PublicationFailureCode.INVALID_GRANT:
                self.repository.mark_reauthorization_required(connection, error.code.value)
            raise OAuthFlowError(error.code, str(error)) from error
        self.repository.store_access_token(connection, tokens.access_token, tokens.expires_at)
        if connection.status == ConnectionStatus.REAUTHORIZATION_REQUIRED.value:
            connection.status = ConnectionStatus.CONNECTED.value
            connection.error_code = None
        return tokens.access_token

    async def verify_channel(self, connection: YouTubeConnection) -> YouTubeChannel:
        """Re-resolve the channel and refuse a connection that has moved.

        A refresh credential can outlive the channel it was granted for. Binding
        every publication to a re-verified channel ID is what stops a video
        being uploaded to a channel the user did not choose.
        """
        token = await self.access_token_for(connection)
        try:
            identity = await self.provider.fetch_channel(access_token=token)
        except YouTubeProviderError as error:
            raise OAuthFlowError(error.code, str(error)) from error
        if identity.channel_id != connection.channel_id:
            self.repository.mark_reauthorization_required(
                connection, PublicationFailureCode.CHANNEL_MISMATCH.value
            )
            raise OAuthFlowError(
                PublicationFailureCode.CHANNEL_MISMATCH,
                "the stored credential now authorizes a different YouTube channel",
            )
        connection.last_verified_at = datetime.now(UTC)
        return YouTubeChannel(
            channel_id=identity.channel_id,
            title=identity.title,
            thumbnail_url=identity.thumbnail_url,
            custom_url=identity.custom_url,
            supports_custom_thumbnails=identity.supports_custom_thumbnails,
        )

    async def disconnect(self, connection: YouTubeConnection, *, revoke: bool = True) -> None:
        """Revoke at Google when possible, then forget the credential locally.

        A revocation failure never blocks the local disconnect: the user asked
        for the channel to be removed, and leaving a decryptable refresh token
        behind because Google was unreachable would be the worse outcome.
        """
        if revoke and connection.credential_present:
            try:
                token = self.repository.refresh_token_for(connection)
                await self.provider.revoke(token=token)
            except (YouTubeProviderError, PublicationStateError):
                pass
        self.repository.disconnect(connection, revoked=revoke)


def connection_projection(connection: YouTubeConnection) -> YouTubeConnectionContract:
    """Project a connection row into its credential-free contract."""
    return YouTubeConnectionContract(
        connection_id=connection.id,
        owner_subject=connection.owner_subject,
        channel=YouTubeChannel(
            channel_id=connection.channel_id,
            title=connection.channel_title or connection.channel_id,
            thumbnail_url=connection.channel_thumbnail_url or "",
            custom_url=connection.custom_url or "",
        ),
        status=ConnectionStatus(connection.status),
        granted_scopes=list(connection.granted_scopes or []),
        credential_expires_at=connection.credential_expires_at,
        encryption_key_version=connection.encryption_key_version or "",
        last_verified_at=connection.last_verified_at,
        created_at=connection.created_at,
        updated_at=connection.updated_at,
    )
