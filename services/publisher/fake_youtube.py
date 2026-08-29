"""A deterministic, credential-free YouTube provider.

Everything the real adapter does, done in memory with no network, no clock
dependence and no randomness. It is what local development, every unit test and
the two acceptance tests run against, and it is deliberately *strict*: it
enforces the same chunk-alignment, offset and duplication rules YouTube does, so
a pipeline bug shows up here rather than against a real channel.

Failure injection is explicit and declarative. A test asks for an interrupted
chunk, an ambiguous final response, an expired session, a caption conflict, a
thumbnail refusal or an exhausted quota by setting a field on
:class:`FakeYouTubeState`; nothing is monkeypatched.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from services.publisher import youtube as capabilities
from services.publisher.contracts import (
    CaptionTrack,
    ChannelIdentity,
    OAuthTokens,
    ProcessingSnapshot,
    ProviderCall,
    ResumableSession,
    ThumbnailResult,
    UploadStatus,
    VideoMetadata,
    VideoSnapshot,
    YouTubeProviderError,
)
from services.publisher.credentials import SecretValue
from services.publisher.providers import provider_error
from vidgen.contracts.publication import PublicationFailureCode

#: A fixed instant, so a fake run is reproducible byte for byte.
FAKE_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
FAKE_CHANNEL_ID = "UCfakevidgenchannel0001"
FAKE_CLIENT_ID = "fake-client-id.apps.googleusercontent.com"


def _deterministic_id(prefix: str, seed: str) -> str:
    """A stable, URL-safe pseudo-ID derived from its inputs."""
    digest = hashlib.sha256(f"{prefix}:{seed}".encode()).hexdigest()
    return f"{prefix}{digest[:16]}"


@dataclass
class FakeVideo:
    video_id: str
    metadata: VideoMetadata
    privacy_status: str
    upload_status: str = "uploaded"
    processing_status: str = capabilities.ProcessingStatus.PROCESSING.value
    parts_total: int = 4
    parts_processed: int = 0
    publish_at: datetime | None = None
    failure_reason: str = ""
    captions: dict[tuple[str, str], CaptionTrack] = field(default_factory=dict)
    thumbnail_sha256: str = ""


@dataclass
class FakeSession:
    session_id: str
    uri: str
    total_bytes: int
    media_type: str
    metadata: VideoMetadata
    confirmed_offset: int = 0
    video_id: str | None = None
    expired: bool = False
    cancelled: bool = False
    #: Bytes the server accepted but did not acknowledge, because the response
    #: was lost. A status query reveals them; the local checkpoint does not.
    unacknowledged: int = 0


@dataclass
class FakeYouTubeState:
    """The mutable world the fake provider serves, plus its failure switches."""

    channel_id: str = FAKE_CHANNEL_ID
    channel_title: str = "VidGen Test Channel"
    channel_thumbnail_url: str = "https://yt3.ggpht.com/fake/vidgen-test-channel.jpg"
    supports_custom_thumbnails: bool = True
    granted_scopes: tuple[str, ...] = capabilities.REQUIRED_SCOPES
    #: Access-token lifetime in seconds, so expiry handling is testable.
    access_token_lifetime_seconds: int = 3600

    videos: dict[str, FakeVideo] = field(default_factory=dict)
    sessions: dict[str, FakeSession] = field(default_factory=dict)
    #: Counts per operation, so a test can assert "exactly one video created".
    calls: dict[str, int] = field(default_factory=dict)
    quota_units: int = 0

    # -- failure injection ---------------------------------------------------
    #: Refuse the token exchange or refresh with ``invalid_grant``.
    invalid_grant: bool = False
    #: Report a narrower scope set than required, to exercise INSUFFICIENT_SCOPE.
    restricted_scopes: tuple[str, ...] | None = None
    #: Fail the chunk upload that would cross this offset, after accepting the
    #: bytes. The pipeline must recover the true offset by querying the session.
    interrupt_after_offset: int | None = None
    #: How many times ``interrupt_after_offset`` fires before it stops.
    interrupt_remaining: int = 1
    #: Accept the final chunk but lose the response. A status query then reveals
    #: a completed session with a video ID.
    lose_final_response: bool = False
    #: Accept the final chunk, lose the response, and let the session expire, so
    #: the outcome is genuinely unknowable.
    ambiguous_completion: bool = False
    #: Every session query and chunk upload reports the session as gone.
    expire_sessions: bool = False
    #: Refuse ``videos.insert`` with ``quotaExceeded``.
    quota_exhausted: bool = False
    #: Refuse ``thumbnails.set`` with a channel-capability 403.
    thumbnails_forbidden: bool = False
    #: Refuse a non-private visibility transition, as an unverified API project.
    privacy_restricted: bool = False
    #: Processing outcome the fake reports once polling has run this many times.
    processing_polls_until_terminal: int = 2
    terminal_processing_status: str = capabilities.ProcessingStatus.SUCCEEDED.value

    def record(self, operation: str, units: int = 0) -> None:
        self.calls[operation] = self.calls.get(operation, 0) + 1
        self.quota_units += units

    def count(self, operation: str) -> int:
        return self.calls.get(operation, 0)


class FakeYouTubeProvider:
    """A deterministic in-memory :class:`~services.publisher.contracts.YouTubeProvider`."""

    name = capabilities.FAKE_PROVIDER_NAME

    def __init__(self, state: FakeYouTubeState | None = None) -> None:
        self.state = state or FakeYouTubeState()

    # -- OAuth ---------------------------------------------------------------
    def authorization_url(
        self, *, redirect_uri: str, scopes: tuple[str, ...], state: str, code_challenge: str
    ) -> str:
        # Deliberately Google's real endpoint: the URL contract test asserts the
        # fake and the adapter agree on shape, so a local connect flow exercises
        # the same validation a production one does.
        query = "&".join(
            (
                f"client_id={FAKE_CLIENT_ID}",
                f"redirect_uri={redirect_uri}",
                "response_type=code",
                f"scope={'%20'.join(scopes)}",
                f"state={state}",
                f"code_challenge={code_challenge}",
                f"code_challenge_method={capabilities.PKCE_CODE_CHALLENGE_METHOD}",
                f"access_type={capabilities.OAUTH_ACCESS_TYPE}",
                f"prompt={capabilities.OAUTH_PROMPT}",
                "include_granted_scopes=true",
            )
        )
        return f"{capabilities.OAUTH_AUTHORIZATION_URL}?{query}"

    def _tokens(self, *, with_refresh: bool) -> OAuthTokens:
        if self.state.invalid_grant:
            raise provider_error(
                http_status=400,
                reason=capabilities.REASON_INVALID_GRANT,
                operation="oauth.token",
                summary="the refresh credential was revoked or has expired",
            )
        scopes = self.state.restricted_scopes or self.state.granted_scopes
        return OAuthTokens(
            access_token=SecretValue("fake-access-token"),
            refresh_token=SecretValue("fake-refresh-token") if with_refresh else None,
            expires_at=FAKE_EPOCH + timedelta(seconds=self.state.access_token_lifetime_seconds),
            granted_scopes=tuple(scopes),
            call=ProviderCall(operation="oauth.token", http_status=200),
        )

    async def exchange_code(
        self, *, code: SecretValue, redirect_uri: str, code_verifier: SecretValue
    ) -> OAuthTokens:
        self.state.record("oauth.exchange")
        if not code.reveal() or not code_verifier.reveal():
            raise provider_error(
                http_status=400,
                reason="invalid_request",
                operation="oauth.token",
                summary="the authorization code exchange requires a code and a PKCE verifier",
            )
        return self._tokens(with_refresh=True)

    async def refresh_access_token(self, *, refresh_token: SecretValue) -> OAuthTokens:
        self.state.record("oauth.refresh")
        return self._tokens(with_refresh=False)

    async def revoke(self, *, token: SecretValue) -> ProviderCall:
        self.state.record("oauth.revoke")
        return ProviderCall(operation="oauth.revoke", http_status=200)

    # -- channel -------------------------------------------------------------
    async def fetch_channel(self, *, access_token: SecretValue) -> ChannelIdentity:
        units = capabilities.CURRENT_QUOTA_PROFILE.cost("channels.list")
        self.state.record("channels.list", units)
        return ChannelIdentity(
            channel_id=self.state.channel_id,
            title=self.state.channel_title,
            thumbnail_url=self.state.channel_thumbnail_url,
            supports_custom_thumbnails=self.state.supports_custom_thumbnails,
            call=ProviderCall(
                operation="channels.list",
                http_status=200,
                provider_request_id="fake-channels-list",
                quota_units=units,
            ),
        )

    # -- resumable upload ----------------------------------------------------
    async def initialize_resumable_upload(
        self,
        *,
        access_token: SecretValue,
        metadata: VideoMetadata,
        total_bytes: int,
        media_type: str,
    ) -> ResumableSession:
        units = capabilities.CURRENT_QUOTA_PROFILE.cost("videos.insert")
        self.state.record("videos.insert.initialize", units)
        if self.state.quota_exhausted:
            raise provider_error(
                http_status=403,
                reason=capabilities.REASON_QUOTA_EXCEEDED,
                operation="videos.insert",
                summary="the channel's daily quota is exhausted",
                quota_units=units,
            )
        if metadata.privacy_status != capabilities.PrivacyStatus.PRIVATE.value:
            raise provider_error(
                http_status=400,
                reason="invalidVideoMetadata",
                operation="videos.insert",
                summary="the fake provider only accepts a private initial upload",
            )
        if total_bytes <= 0 or total_bytes > capabilities.MAX_VIDEO_BYTES:
            raise provider_error(
                http_status=400,
                reason="mediaBodyRequired",
                operation="videos.insert",
                summary="the declared media length is outside YouTube's accepted range",
            )
        if media_type not in capabilities.ACCEPTED_VIDEO_MEDIA_TYPES:
            raise provider_error(
                http_status=400,
                reason="invalidMediaType",
                operation="videos.insert",
                summary=f"{media_type} is not an accepted video media type",
            )
        session_id = _deterministic_id(
            "sess", f"{metadata.title}:{total_bytes}:{len(self.state.sessions)}"
        )
        uri = f"https://fake-upload.googleapis.com/resumable/{session_id}"
        self.state.sessions[uri] = FakeSession(
            session_id=session_id,
            uri=uri,
            total_bytes=total_bytes,
            media_type=media_type,
            metadata=metadata,
        )
        return ResumableSession(
            upload_uri=SecretValue(uri),
            expires_at=FAKE_EPOCH + timedelta(seconds=capabilities.RESUMABLE_SESSION_TTL_SECONDS),
            call=ProviderCall(
                operation="videos.insert",
                http_status=200,
                provider_request_id=f"fake-init-{session_id}",
                quota_units=units,
            ),
        )

    def _session(self, upload_uri: SecretValue, operation: str) -> FakeSession:
        session = self.state.sessions.get(upload_uri.reveal())
        if session is None or session.cancelled:
            raise provider_error(
                http_status=capabilities.NOT_FOUND_STATUS,
                operation=operation,
                summary="the resumable upload session does not exist",
            )
        if session.expired or self.state.expire_sessions:
            session.expired = True
            raise provider_error(
                http_status=capabilities.GONE_STATUS,
                operation=operation,
                summary="the resumable upload session has expired",
            )
        return session

    async def query_upload_status(
        self, *, access_token: SecretValue, upload_uri: SecretValue, total_bytes: int
    ) -> UploadStatus:
        self.state.record("videos.insert.status")
        session = self._session(upload_uri, "videos.insert.status")
        # A status query is the only truthful source of the offset: it includes
        # bytes the server accepted whose response the client never saw.
        confirmed = session.confirmed_offset + session.unacknowledged
        session.confirmed_offset = confirmed
        session.unacknowledged = 0
        completed = confirmed >= session.total_bytes and session.video_id is not None
        return UploadStatus(
            confirmed_offset=confirmed,
            completed=completed,
            video_id=session.video_id if completed else None,
            call=ProviderCall(
                operation="videos.insert.status",
                http_status=200 if completed else capabilities.RESUME_INCOMPLETE_STATUS,
            ),
        )

    async def upload_chunk(
        self,
        *,
        access_token: SecretValue,
        upload_uri: SecretValue,
        chunk: bytes,
        start: int,
        total_bytes: int,
    ) -> UploadStatus:
        self.state.record("videos.insert.chunk")
        session = self._session(upload_uri, "videos.insert.chunk")
        if total_bytes != session.total_bytes:
            raise provider_error(
                http_status=400,
                operation="videos.insert.chunk",
                summary="the declared total size does not match the session",
            )
        expected = session.confirmed_offset + session.unacknowledged
        if start != expected:
            # YouTube answers a misaligned chunk with 308 and the true range
            # rather than accepting it, which is what stops a client that
            # trusted a stale local checkpoint from corrupting the upload.
            # Reporting the range also folds in bytes whose response was lost.
            session.confirmed_offset = expected
            session.unacknowledged = 0
            return UploadStatus(
                confirmed_offset=expected,
                completed=False,
                call=ProviderCall(
                    operation="videos.insert.chunk",
                    http_status=capabilities.RESUME_INCOMPLETE_STATUS,
                ),
            )
        end = start + len(chunk)
        if end > session.total_bytes:
            raise provider_error(
                http_status=400,
                operation="videos.insert.chunk",
                summary="the chunk extends beyond the declared total size",
            )
        is_final = end == session.total_bytes
        if not is_final and len(chunk) % capabilities.RESUMABLE_CHUNK_GRANULARITY != 0:
            raise provider_error(
                http_status=400,
                operation="videos.insert.chunk",
                summary="a non-final chunk must be a multiple of 256 KiB",
            )

        interrupt = self.state.interrupt_after_offset
        if (
            interrupt is not None
            and self.state.interrupt_remaining > 0
            and start < interrupt <= end
        ):
            # The bytes are accepted; the response is lost. This is exactly the
            # case the pipeline must resolve by asking the server, not by
            # trusting what it last wrote down.
            self.state.interrupt_remaining -= 1
            session.unacknowledged += len(chunk)
            raise provider_error(
                http_status=503,
                operation="videos.insert.chunk",
                summary="the connection dropped after the chunk was sent",
            )

        if is_final:
            video_id = _deterministic_id("vid", session.session_id)
            if session.video_id is None:
                self.state.videos[video_id] = FakeVideo(
                    video_id=video_id,
                    metadata=session.metadata,
                    privacy_status=session.metadata.privacy_status,
                    publish_at=session.metadata.publish_at,
                    parts_total=4,
                )
                session.video_id = video_id
            session.confirmed_offset = session.total_bytes
            session.unacknowledged = 0
            if self.state.ambiguous_completion:
                # Accepted, response lost, and the session then vanishes: the
                # system genuinely cannot prove whether a video exists.
                session.expired = True
                raise provider_error(
                    http_status=503,
                    operation="videos.insert.chunk",
                    summary="the final chunk response was lost",
                )
            if self.state.lose_final_response:
                self.state.lose_final_response = False
                raise provider_error(
                    http_status=503,
                    operation="videos.insert.chunk",
                    summary="the final chunk response was lost",
                )
            return UploadStatus(
                confirmed_offset=session.total_bytes,
                completed=True,
                video_id=session.video_id,
                call=ProviderCall(
                    operation="videos.insert.chunk",
                    http_status=200,
                    provider_request_id=f"fake-final-{session.session_id}",
                ),
            )

        session.confirmed_offset = end
        session.unacknowledged = 0
        return UploadStatus(
            confirmed_offset=end,
            completed=False,
            call=ProviderCall(
                operation="videos.insert.chunk",
                http_status=capabilities.RESUME_INCOMPLETE_STATUS,
            ),
        )

    async def cancel_resumable_upload(
        self, *, access_token: SecretValue, upload_uri: SecretValue
    ) -> ProviderCall:
        self.state.record("videos.insert.cancel")
        session = self.state.sessions.get(upload_uri.reveal())
        if session is not None:
            session.cancelled = True
        return ProviderCall(operation="videos.insert.cancel", http_status=499)

    # -- video ---------------------------------------------------------------
    def _video(self, video_id: str, operation: str) -> FakeVideo:
        video = self.state.videos.get(video_id)
        if video is None:
            raise provider_error(
                http_status=capabilities.NOT_FOUND_STATUS,
                operation=operation,
                summary="the video does not exist on this channel",
            )
        return video

    async def fetch_video(self, *, access_token: SecretValue, video_id: str) -> VideoSnapshot:
        units = capabilities.CURRENT_QUOTA_PROFILE.cost("videos.list")
        self.state.record("videos.list", units)
        video = self._video(video_id, "videos.list")
        return VideoSnapshot(
            video_id=video.video_id,
            privacy_status=video.privacy_status,
            upload_status=video.upload_status,
            made_for_kids=video.metadata.made_for_kids,
            contains_synthetic_media=video.metadata.contains_synthetic_media,
            embeddable=video.metadata.embeddable,
            publish_at=video.publish_at,
            title=video.metadata.title,
            failure_reason=video.failure_reason,
            call=ProviderCall(operation="videos.list", http_status=200, quota_units=units),
        )

    async def fetch_processing_status(
        self, *, access_token: SecretValue, video_id: str
    ) -> ProcessingSnapshot:
        units = capabilities.CURRENT_QUOTA_PROFILE.cost("videos.list")
        self.state.record("videos.processing", units)
        video = self._video(video_id, "videos.list")
        video.parts_processed = min(video.parts_total, video.parts_processed + 2)
        polls = self.state.count("videos.processing")
        if polls >= self.state.processing_polls_until_terminal:
            video.processing_status = self.state.terminal_processing_status
            video.parts_processed = video.parts_total
            if video.processing_status != capabilities.ProcessingStatus.SUCCEEDED.value:
                video.failure_reason = "transcodeFailed"
                video.upload_status = capabilities.UploadStatus.FAILED.value
            else:
                video.upload_status = capabilities.UploadStatus.PROCESSED.value
        return ProcessingSnapshot(
            video_id=video.video_id,
            processing_status=video.processing_status,
            parts_total=video.parts_total,
            parts_processed=video.parts_processed,
            failure_reason=video.failure_reason,
            upload_status=video.upload_status,
            call=ProviderCall(operation="videos.list", http_status=200, quota_units=units),
        )

    async def update_metadata(
        self, *, access_token: SecretValue, video_id: str, metadata: VideoMetadata
    ) -> VideoSnapshot:
        units = capabilities.CURRENT_QUOTA_PROFILE.cost("videos.update")
        self.state.record("videos.update", units)
        video = self._video(video_id, "videos.update")
        video.metadata = metadata
        return VideoSnapshot(
            video_id=video.video_id,
            privacy_status=video.privacy_status,
            upload_status=video.upload_status,
            made_for_kids=metadata.made_for_kids,
            contains_synthetic_media=metadata.contains_synthetic_media,
            embeddable=metadata.embeddable,
            publish_at=video.publish_at,
            title=metadata.title,
            call=ProviderCall(operation="videos.update", http_status=200, quota_units=units),
        )

    async def update_visibility(
        self,
        *,
        access_token: SecretValue,
        video_id: str,
        privacy_status: str,
        publish_at: datetime | None,
        notify_subscribers: bool,
    ) -> VideoSnapshot:
        units = capabilities.CURRENT_QUOTA_PROFILE.cost("videos.update")
        self.state.record("videos.visibility", units)
        video = self._video(video_id, "videos.update")
        if privacy_status not in {status.value for status in capabilities.PrivacyStatus}:
            raise provider_error(
                http_status=400,
                operation="videos.update",
                summary=f"{privacy_status!r} is not a supported privacy state",
            )
        if self.state.privacy_restricted and privacy_status != (
            capabilities.PrivacyStatus.PRIVATE.value
        ):
            # An unverified API project keeps the video private, and says so.
            raise provider_error(
                http_status=403,
                reason=capabilities.REASON_PRIVACY_RESTRICTED,
                operation="videos.update",
                summary="this API project may only publish private videos",
                quota_units=units,
            )
        video.privacy_status = privacy_status
        video.publish_at = publish_at
        return VideoSnapshot(
            video_id=video.video_id,
            privacy_status=video.privacy_status,
            upload_status=video.upload_status,
            made_for_kids=video.metadata.made_for_kids,
            contains_synthetic_media=video.metadata.contains_synthetic_media,
            embeddable=video.metadata.embeddable,
            publish_at=video.publish_at,
            title=video.metadata.title,
            call=ProviderCall(operation="videos.update", http_status=200, quota_units=units),
        )

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
    ) -> CaptionTrack:
        units = capabilities.CURRENT_QUOTA_PROFILE.cost("captions.insert")
        self.state.record("captions.insert", units)
        video = self._video(video_id, "captions.insert")
        if len(content) > capabilities.MAX_CAPTION_BYTES:
            raise provider_error(
                http_status=400,
                operation="captions.insert",
                summary="the caption file exceeds YouTube's size limit",
            )
        key = (language, name)
        if key in video.captions:
            raise provider_error(
                http_status=capabilities.CONFLICT_STATUS,
                reason=capabilities.REASON_CAPTION_EXISTS,
                operation="captions.insert",
                summary="a caption track with this language and name already exists",
                quota_units=units,
            )
        track = CaptionTrack(
            caption_id=_deterministic_id("cap", f"{video_id}:{language}:{name}"),
            language=language,
            name=name,
            call=ProviderCall(operation="captions.insert", http_status=200, quota_units=units),
        )
        video.captions[key] = track
        return track

    async def list_captions(
        self, *, access_token: SecretValue, video_id: str
    ) -> tuple[CaptionTrack, ...]:
        units = capabilities.CURRENT_QUOTA_PROFILE.cost("captions.list")
        self.state.record("captions.list", units)
        video = self._video(video_id, "captions.list")
        return tuple(video.captions.values())

    async def set_thumbnail(
        self, *, access_token: SecretValue, video_id: str, content: bytes, media_type: str
    ) -> ThumbnailResult:
        units = capabilities.CURRENT_QUOTA_PROFILE.cost("thumbnails.set")
        self.state.record("thumbnails.set", units)
        video = self._video(video_id, "thumbnails.set")
        if self.state.thumbnails_forbidden:
            raise provider_error(
                http_status=403,
                operation="thumbnails.set",
                summary="this channel is not permitted to set a custom thumbnail",
                quota_units=units,
            )
        if len(content) > capabilities.MAX_THUMBNAIL_BYTES:
            raise provider_error(
                http_status=400,
                operation="thumbnails.set",
                summary="the thumbnail exceeds YouTube's 2 MiB limit",
            )
        if media_type not in capabilities.ACCEPTED_THUMBNAIL_MEDIA_TYPES:
            raise provider_error(
                http_status=400,
                operation="thumbnails.set",
                summary=f"{media_type} is not an accepted thumbnail media type",
            )
        video.thumbnail_sha256 = hashlib.sha256(content).hexdigest()
        return ThumbnailResult(
            video_id=video_id,
            url=f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg",
            width=capabilities.RECOMMENDED_THUMBNAIL_WIDTH,
            height=capabilities.RECOMMENDED_THUMBNAIL_HEIGHT,
            call=ProviderCall(operation="thumbnails.set", http_status=200, quota_units=units),
        )


#: One process-wide fake world, so an API request and a worker running in the
#: same process see the same fake channel and the same fake videos. Tests that
#: want isolation construct their own :class:`FakeYouTubeState`.
_SHARED_STATE: FakeYouTubeState | None = None


def shared_state() -> FakeYouTubeState:
    """The process-wide fake state used when no explicit one is supplied."""
    global _SHARED_STATE
    if _SHARED_STATE is None:
        _SHARED_STATE = FakeYouTubeState()
    return _SHARED_STATE


def reset_shared_state() -> FakeYouTubeState:
    """Discard the process-wide fake world. Used between tests."""
    global _SHARED_STATE
    _SHARED_STATE = FakeYouTubeState()
    return _SHARED_STATE


def unexpected_state(message: str) -> YouTubeProviderError:
    """A fake-only invariant breach, classified so tests see a real failure."""
    return YouTubeProviderError(PublicationFailureCode.PROVIDER_REJECTED, message)
