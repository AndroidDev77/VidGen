"""The provider-neutral boundary between the publication pipeline and YouTube.

Nothing below this line knows what a ``googleapiclient`` object is. The
production adapter, the deterministic fake and the mocked contract tests all
speak exactly these dataclasses, which is what lets the whole pipeline run
offline and lets a future provider be added without touching the pipeline.

Two deliberate shapes:

* **Credentials are :class:`~services.publisher.credentials.SecretValue`.** An
  access token is passed as an opaque wrapper that will not print itself, so an
  exception raised deep inside an HTTP client cannot leak one into a log.
* **Media is a range reader, not bytes.** A final MP4 can be gigabytes; the
  pipeline never materialises one. A :class:`ChunkSource` yields exactly the
  window a chunk needs, from the local filesystem or from Blob Storage.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from services.publisher.credentials import SecretValue
from vidgen.contracts.publication import PublicationFailureCode


class YouTubeProviderError(RuntimeError):
    """A classified failure from a YouTube operation.

    The message is safe to persist and to render: it never contains a token, a
    session URI, an authorization code or a raw provider payload.
    """

    def __init__(
        self,
        code: PublicationFailureCode,
        message: str,
        *,
        http_status: int | None = None,
        reason: str = "",
        retryable: bool = False,
        remediation: str = "",
        provider_request_id: str = "",
        quota_units: int = 0,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.reason = reason
        self.retryable = retryable
        self.remediation = remediation
        self.provider_request_id = provider_request_id
        self.quota_units = quota_units


class ChunkSource(Protocol):
    """A readable, seekable window onto the media being uploaded."""

    @property
    def byte_size(self) -> int: ...

    @property
    def media_type(self) -> str: ...

    def read_range(self, start: int, length: int) -> bytes:
        """Return exactly ``length`` bytes from ``start``, or fewer at the end."""
        ...


@dataclass(frozen=True, slots=True)
class ProviderCall:
    """The instrumentation every provider operation reports back.

    ``quota_units`` is a rate-limit quantity, not money. The pipeline records it
    on the T23 provider attempt with a zero monetary cost.
    """

    operation: str
    http_status: int | None = None
    provider_request_id: str = ""
    quota_units: int = 0
    retry_count: int = 0
    latency_ms: int = 0


@dataclass(frozen=True, slots=True)
class OAuthTokens:
    """The result of an authorization-code exchange or a refresh.

    ``refresh_token`` is absent on a refresh response: Google only issues one on
    the first offline grant, so the caller keeps the one it already sealed.
    """

    access_token: SecretValue
    expires_at: datetime
    granted_scopes: tuple[str, ...]
    refresh_token: SecretValue | None = None
    token_type: str = "Bearer"
    call: ProviderCall = field(default_factory=lambda: ProviderCall(operation="oauth.token"))


@dataclass(frozen=True, slots=True)
class ChannelIdentity:
    """The authenticated channel, resolved from YouTube after authorization."""

    channel_id: str
    title: str
    thumbnail_url: str = ""
    custom_url: str = ""
    #: ``None`` when YouTube did not tell us. Never guessed.
    supports_custom_thumbnails: bool | None = None
    call: ProviderCall = field(default_factory=lambda: ProviderCall(operation="channels.list"))


@dataclass(frozen=True, slots=True)
class VideoMetadata:
    """The snippet and status a write sends. Validated before it gets here."""

    title: str
    description: str
    tags: tuple[str, ...]
    category_id: str
    default_language: str
    privacy_status: str
    made_for_kids: bool
    contains_synthetic_media: bool
    embeddable: bool
    notify_subscribers: bool
    publish_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ResumableSession:
    """A created resumable upload session.

    ``upload_uri`` is a bearer credential: anyone holding it can append bytes to
    this upload. It is sealed before it is persisted and never leaves the
    publisher.
    """

    upload_uri: SecretValue
    expires_at: datetime | None = None
    call: ProviderCall = field(default_factory=lambda: ProviderCall(operation="videos.insert"))


@dataclass(frozen=True, slots=True)
class UploadStatus:
    """The answer to "what does the server think it has?".

    Returned both by a status query and by a chunk upload, so the pipeline
    always advances on a *server-confirmed* offset.
    """

    #: The first byte the server has not yet received. ``0`` means nothing.
    confirmed_offset: int
    completed: bool
    #: Present exactly when ``completed`` and the final response was seen.
    video_id: str | None = None
    #: True when the session no longer exists and cannot be resumed.
    expired: bool = False
    call: ProviderCall = field(
        default_factory=lambda: ProviderCall(operation="videos.insert.status")
    )


@dataclass(frozen=True, slots=True)
class VideoSnapshot:
    """A bounded projection of one ``videos`` resource."""

    video_id: str
    privacy_status: str
    upload_status: str = ""
    made_for_kids: bool | None = None
    contains_synthetic_media: bool | None = None
    embeddable: bool | None = None
    publish_at: datetime | None = None
    title: str = ""
    failure_reason: str = ""
    rejection_reason: str = ""
    call: ProviderCall = field(default_factory=lambda: ProviderCall(operation="videos.list"))


@dataclass(frozen=True, slots=True)
class ProcessingSnapshot:
    """A bounded projection of ``processingDetails``."""

    video_id: str
    #: One of the capability registry's ``ProcessingStatus`` values, or "".
    processing_status: str
    parts_total: int | None = None
    parts_processed: int | None = None
    failure_reason: str = ""
    upload_status: str = ""
    call: ProviderCall = field(default_factory=lambda: ProviderCall(operation="videos.list"))

    @property
    def percent(self) -> float | None:
        if not self.parts_total:
            return None
        processed = self.parts_processed or 0
        return min(100.0, max(0.0, 100.0 * processed / self.parts_total))


@dataclass(frozen=True, slots=True)
class CaptionTrack:
    """One caption track on the published video."""

    caption_id: str
    language: str
    name: str
    is_draft: bool = False
    track_kind: str = "standard"
    call: ProviderCall = field(default_factory=lambda: ProviderCall(operation="captions.list"))


@dataclass(frozen=True, slots=True)
class ThumbnailResult:
    """The outcome of ``thumbnails.set``. YouTube returns no resource ID."""

    video_id: str
    #: The default thumbnail URL YouTube reports back, when present.
    url: str = ""
    width: int | None = None
    height: int | None = None
    call: ProviderCall = field(default_factory=lambda: ProviderCall(operation="thumbnails.set"))


class YouTubeProvider(Protocol):
    """Everything the publication pipeline may ask a YouTube-shaped provider.

    Implemented by the production Data API adapter and by the deterministic
    fake. Every method either returns its dataclass or raises
    :class:`YouTubeProviderError` with a classified code.
    """

    #: ``"youtube"`` or ``"fake-youtube"``. Recorded on every provider attempt.
    name: str

    # -- OAuth ---------------------------------------------------------------
    def authorization_url(
        self,
        *,
        redirect_uri: str,
        scopes: tuple[str, ...],
        state: str,
        code_challenge: str,
    ) -> str:
        """Build the user-facing authorization URL. Never carries a secret."""
        ...

    async def exchange_code(
        self, *, code: SecretValue, redirect_uri: str, code_verifier: SecretValue
    ) -> OAuthTokens:
        """Exchange an authorization code. Backend only; PKCE always sent."""
        ...

    async def refresh_access_token(self, *, refresh_token: SecretValue) -> OAuthTokens: ...

    async def revoke(self, *, token: SecretValue) -> ProviderCall: ...

    # -- channel -------------------------------------------------------------
    async def fetch_channel(self, *, access_token: SecretValue) -> ChannelIdentity:
        """Resolve the authenticated channel. Never trusts a client-sent ID."""
        ...

    # -- resumable upload ----------------------------------------------------
    async def initialize_resumable_upload(
        self,
        *,
        access_token: SecretValue,
        metadata: VideoMetadata,
        total_bytes: int,
        media_type: str,
    ) -> ResumableSession: ...

    async def query_upload_status(
        self, *, access_token: SecretValue, upload_uri: SecretValue, total_bytes: int
    ) -> UploadStatus: ...

    async def upload_chunk(
        self,
        *,
        access_token: SecretValue,
        upload_uri: SecretValue,
        chunk: bytes,
        start: int,
        total_bytes: int,
    ) -> UploadStatus: ...

    async def cancel_resumable_upload(
        self, *, access_token: SecretValue, upload_uri: SecretValue
    ) -> ProviderCall: ...

    # -- video ---------------------------------------------------------------
    async def fetch_video(self, *, access_token: SecretValue, video_id: str) -> VideoSnapshot: ...

    async def fetch_processing_status(
        self, *, access_token: SecretValue, video_id: str
    ) -> ProcessingSnapshot: ...

    async def update_metadata(
        self, *, access_token: SecretValue, video_id: str, metadata: VideoMetadata
    ) -> VideoSnapshot: ...

    async def update_visibility(
        self, *, access_token: SecretValue, video_id: str, metadata: VideoMetadata
    ) -> VideoSnapshot:
        """Write the complete ``status`` part with the requested privacy.

        The whole resource, never a single field: ``videos.update`` replaces the
        part it is given, so sending ``privacyStatus`` alone would delete
        ``containsSyntheticMedia``, ``selfDeclaredMadeForKids`` and
        ``embeddable`` at the exact moment the video becomes visible.
        """
        ...

    # -- captions and thumbnails --------------------------------------------
    async def insert_caption(
        self,
        *,
        access_token: SecretValue,
        video_id: str,
        language: str,
        name: str,
        content: bytes,
        media_type: str,
    ) -> CaptionTrack: ...

    async def list_captions(
        self, *, access_token: SecretValue, video_id: str
    ) -> tuple[CaptionTrack, ...]: ...

    async def set_thumbnail(
        self, *, access_token: SecretValue, video_id: str, content: bytes, media_type: str
    ) -> ThumbnailResult: ...
