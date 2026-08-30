"""The production YouTube Data API v3 adapter.

Plain HTTPS against the endpoints in :mod:`services.publisher.youtube`, with
``httpx``. No Google client library is used, and no Google client-library object
ever escapes this module: every response is projected into the small dataclasses
in :mod:`services.publisher.contracts` before it is returned, so the pipeline
cannot accidentally depend on a provider payload shape.

The resumable upload protocol is implemented exactly as documented:

* ``POST .../videos?uploadType=resumable&part=snippet,status`` with the metadata
  as the JSON body and ``X-Upload-Content-Length``/``-Type`` headers returns a
  session URI in ``Location``.
* Each chunk is a ``PUT`` to that URI with
  ``Content-Range: bytes <start>-<end>/<total>``.
* ``308 Resume Incomplete`` means "keep going", and its ``Range`` header carries
  the last byte the server actually has. That value, never a local counter, is
  what the caller advances on.
* A status query is a ``PUT`` with ``Content-Range: bytes */<total>`` and an
  empty body. It is the only way to learn the truth after an interrupted chunk.

Nothing here logs a token, a session URI, an authorization code or a response
body. Errors are raised as classified
:class:`~services.publisher.contracts.YouTubeProviderError` values built by
:func:`services.publisher.providers.provider_error`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx

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
)
from services.publisher.credentials import SecretValue
from services.publisher.providers import provider_error

#: Header YouTube returns its opaque request identifier in.
_REQUEST_ID_HEADERS = ("x-guploader-uploadid", "x-goog-request-id")


def _request_id(response: httpx.Response) -> str:
    for header in _REQUEST_ID_HEADERS:
        value = response.headers.get(header)
        if value:
            return str(value)[:255]
    return ""


def _reason(response: httpx.Response) -> str:
    """Google's structured error reason, or "" when the body is not JSON.

    Only the ``reason`` and ``status`` fields are read. The body itself is never
    stored, logged or returned: it can echo request metadata.
    """
    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error")
    if isinstance(error, str):
        return error[:128]
    if not isinstance(error, dict):
        return ""
    errors = error.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        reason = errors[0].get("reason")
        if isinstance(reason, str):
            return reason[:128]
    status = error.get("status")
    return status[:128] if isinstance(status, str) else ""


def _fail(response: httpx.Response, operation: str, *, quota_units: int = 0) -> Exception:
    return provider_error(
        http_status=response.status_code,
        reason=_reason(response),
        operation=operation,
        summary=f"{operation} was refused by YouTube with status {response.status_code}",
        provider_request_id=_request_id(response),
        quota_units=quota_units,
    )


def _confirmed_offset(response: httpx.Response) -> int:
    """The next byte to send, from a ``308`` response's ``Range`` header.

    ``Range: bytes=0-262143`` means the server holds bytes 0..262143 inclusive,
    so the next byte is 262144. An absent header means the server holds nothing.
    """
    header = response.headers.get("range") or response.headers.get("Range")
    if not header:
        return 0
    _, _, span = header.partition("=")
    _, _, last = span.partition("-")
    try:
        return int(last) + 1
    except ValueError:
        return 0


def _snippet_and_status(metadata: VideoMetadata) -> dict[str, Any]:
    """The ``videos.insert``/``videos.update`` body.

    ``containsSyntheticMedia`` is always present: VidGen output is animated and
    AI generated, and the selected capability profile supports the field, so
    omitting it would be an undisclosed synthetic upload.
    """
    status: dict[str, Any] = {
        "privacyStatus": metadata.privacy_status,
        "selfDeclaredMadeForKids": metadata.made_for_kids,
        "containsSyntheticMedia": metadata.contains_synthetic_media,
        "embeddable": metadata.embeddable,
    }
    if metadata.publish_at is not None:
        status["publishAt"] = metadata.publish_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return {
        "snippet": {
            "title": metadata.title,
            "description": metadata.description,
            "tags": list(metadata.tags),
            "categoryId": metadata.category_id,
            "defaultLanguage": metadata.default_language,
            "defaultAudioLanguage": metadata.default_language,
        },
        "status": status,
    }


def _expires_at(expires_in: object) -> datetime:
    seconds = int(expires_in) if isinstance(expires_in, (int, float, str)) else 3600
    return datetime.now(UTC) + timedelta(seconds=max(0, seconds))


class YouTubeDataApiProvider:
    """A :class:`~services.publisher.contracts.YouTubeProvider` over HTTPS."""

    name = capabilities.PROVIDER_NAME

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 120.0,
    ) -> None:
        self._client_id = client_id
        #: Held as a SecretValue so an unexpected repr of this adapter cannot
        #: print the client secret.
        self._client_secret = SecretValue(client_secret)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"YouTubeDataApiProvider(client_id={self._client_id!r})"

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @staticmethod
    def _auth(access_token: SecretValue) -> dict[str, str]:
        return {"Authorization": f"Bearer {access_token.reveal()}"}

    # -- OAuth ---------------------------------------------------------------
    def authorization_url(
        self, *, redirect_uri: str, scopes: tuple[str, ...], state: str, code_challenge: str
    ) -> str:
        query = urlencode(
            {
                "client_id": self._client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": " ".join(scopes),
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": capabilities.PKCE_CODE_CHALLENGE_METHOD,
                # Offline access is what returns a refresh token at all.
                "access_type": capabilities.OAUTH_ACCESS_TYPE,
                # Without it Google may omit the refresh token for a user who
                # has authorized before, leaving a connection that cannot renew.
                "prompt": capabilities.OAUTH_PROMPT,
                "include_granted_scopes": "true",
            }
        )
        return f"{capabilities.OAUTH_AUTHORIZATION_URL}?{query}"

    async def _token_request(self, form: dict[str, str], operation: str) -> OAuthTokens:
        response = await self._client.post(
            capabilities.OAUTH_TOKEN_URL,
            data={
                **form,
                "client_id": self._client_id,
                "client_secret": self._client_secret.reveal(),
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if response.status_code >= 400:
            raise _fail(response, operation)
        payload = response.json()
        if not isinstance(payload, dict) or "access_token" not in payload:
            raise provider_error(
                http_status=response.status_code,
                operation=operation,
                summary="the token endpoint returned no access token",
            )
        refresh = payload.get("refresh_token")
        return OAuthTokens(
            access_token=SecretValue(str(payload["access_token"])),
            refresh_token=SecretValue(str(refresh)) if refresh else None,
            expires_at=_expires_at(payload.get("expires_in")),
            granted_scopes=tuple(str(payload.get("scope", "")).split()),
            call=ProviderCall(
                operation=operation,
                http_status=response.status_code,
                provider_request_id=_request_id(response),
            ),
        )

    async def exchange_code(
        self, *, code: SecretValue, redirect_uri: str, code_verifier: SecretValue
    ) -> OAuthTokens:
        return await self._token_request(
            {
                "code": code.reveal(),
                "grant_type": "authorization_code",
                # Must be byte-identical to the URI in the authorization
                # request and to one registered with Google.
                "redirect_uri": redirect_uri,
                "code_verifier": code_verifier.reveal(),
            },
            "oauth.exchange",
        )

    async def refresh_access_token(self, *, refresh_token: SecretValue) -> OAuthTokens:
        return await self._token_request(
            {"refresh_token": refresh_token.reveal(), "grant_type": "refresh_token"},
            "oauth.refresh",
        )

    async def revoke(self, *, token: SecretValue) -> ProviderCall:
        response = await self._client.post(
            capabilities.OAUTH_REVOCATION_URL,
            data={"token": token.reveal()},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        # Google answers an already-invalid token with 400; that is a successful
        # revocation from this system's point of view.
        if response.status_code >= 500:
            raise _fail(response, "oauth.revoke")
        return ProviderCall(
            operation="oauth.revoke",
            http_status=response.status_code,
            provider_request_id=_request_id(response),
        )

    # -- channel -------------------------------------------------------------
    async def fetch_channel(self, *, access_token: SecretValue) -> ChannelIdentity:
        units = capabilities.CURRENT_QUOTA_PROFILE.cost("channels.list")
        response = await self._client.get(
            capabilities.CHANNELS_URL,
            params={"part": ",".join(capabilities.CHANNEL_READ_PARTS), "mine": "true"},
            headers=self._auth(access_token),
        )
        if response.status_code >= 400:
            raise _fail(response, "channels.list", quota_units=units)
        items = response.json().get("items") or []
        if not items:
            raise provider_error(
                http_status=response.status_code,
                operation="channels.list",
                summary="the authorized account owns no YouTube channel",
                quota_units=units,
            )
        item = items[0]
        snippet = item.get("snippet") or {}
        thumbnails = snippet.get("thumbnails") or {}
        default = thumbnails.get("default") or {}
        url = str(default.get("url", ""))
        return ChannelIdentity(
            channel_id=str(item.get("id", "")),
            title=str(snippet.get("title", ""))[:255],
            # Only an https URL is kept; anything else is dropped rather than
            # stored and later rendered in the dashboard.
            thumbnail_url=url if url.startswith("https://") else "",
            custom_url=str(snippet.get("customUrl", ""))[:255],
            call=ProviderCall(
                operation="channels.list",
                http_status=response.status_code,
                provider_request_id=_request_id(response),
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
        response = await self._client.post(
            capabilities.VIDEOS_INSERT_URL,
            params={
                "uploadType": "resumable",
                "part": ",".join(capabilities.VIDEO_INSERT_PARTS),
                # Explicitly false unless the user asked. A default that
                # notified every subscriber would be irreversible.
                "notifySubscribers": str(metadata.notify_subscribers).lower(),
            },
            json=_snippet_and_status(metadata),
            headers={
                **self._auth(access_token),
                "X-Upload-Content-Length": str(total_bytes),
                "X-Upload-Content-Type": media_type,
                "Content-Type": "application/json; charset=UTF-8",
            },
        )
        if response.status_code >= 400:
            raise _fail(response, "videos.insert", quota_units=units)
        location = response.headers.get("location") or response.headers.get("Location")
        if not location:
            raise provider_error(
                http_status=response.status_code,
                operation="videos.insert",
                summary="YouTube accepted the metadata but returned no resumable session URI",
                quota_units=units,
            )
        return ResumableSession(
            upload_uri=SecretValue(location),
            expires_at=datetime.now(UTC)
            + timedelta(seconds=capabilities.RESUMABLE_SESSION_TTL_SECONDS),
            call=ProviderCall(
                operation="videos.insert",
                http_status=response.status_code,
                provider_request_id=_request_id(response),
                quota_units=units,
            ),
        )

    @staticmethod
    def _completed(response: httpx.Response, operation: str) -> UploadStatus:
        payload = response.json()
        video_id = str((payload or {}).get("id", "")) if isinstance(payload, dict) else ""
        if not video_id:
            # A 200 without an ID is exactly the ambiguous case: bytes were
            # accepted, but the identity of what was created is unknown.
            raise provider_error(
                http_status=response.status_code,
                operation=operation,
                summary="YouTube completed the upload without returning a video ID",
                provider_request_id=_request_id(response),
            )
        return UploadStatus(
            confirmed_offset=0,
            completed=True,
            video_id=video_id,
            call=ProviderCall(
                operation=operation,
                http_status=response.status_code,
                provider_request_id=_request_id(response),
            ),
        )

    async def query_upload_status(
        self, *, access_token: SecretValue, upload_uri: SecretValue, total_bytes: int
    ) -> UploadStatus:
        operation = "videos.insert.status"
        response = await self._client.put(
            upload_uri.reveal(),
            headers={
                **self._auth(access_token),
                # The documented "how much do you have?" probe: an empty body
                # with an unknown range.
                "Content-Range": f"bytes */{total_bytes}",
                "Content-Length": "0",
            },
            content=b"",
        )
        if response.status_code == capabilities.RESUME_INCOMPLETE_STATUS:
            offset = _confirmed_offset(response)
            return UploadStatus(
                confirmed_offset=offset,
                completed=False,
                call=ProviderCall(
                    operation=operation,
                    http_status=response.status_code,
                    provider_request_id=_request_id(response),
                ),
            )
        if response.status_code in {200, 201}:
            completed = self._completed(response, operation)
            return UploadStatus(
                confirmed_offset=total_bytes,
                completed=True,
                video_id=completed.video_id,
                call=completed.call,
            )
        if response.status_code in {capabilities.GONE_STATUS, capabilities.NOT_FOUND_STATUS}:
            return UploadStatus(
                confirmed_offset=0,
                completed=False,
                expired=True,
                call=ProviderCall(
                    operation=operation,
                    http_status=response.status_code,
                    provider_request_id=_request_id(response),
                ),
            )
        raise _fail(response, operation)

    async def upload_chunk(
        self,
        *,
        access_token: SecretValue,
        upload_uri: SecretValue,
        chunk: bytes,
        start: int,
        total_bytes: int,
    ) -> UploadStatus:
        operation = "videos.insert.chunk"
        end = start + len(chunk) - 1
        response = await self._client.put(
            upload_uri.reveal(),
            headers={
                **self._auth(access_token),
                "Content-Range": f"bytes {start}-{end}/{total_bytes}",
                "Content-Length": str(len(chunk)),
            },
            content=chunk,
        )
        if response.status_code == capabilities.RESUME_INCOMPLETE_STATUS:
            return UploadStatus(
                confirmed_offset=_confirmed_offset(response),
                completed=False,
                call=ProviderCall(
                    operation=operation,
                    http_status=response.status_code,
                    provider_request_id=_request_id(response),
                ),
            )
        if response.status_code in {200, 201}:
            completed = self._completed(response, operation)
            return UploadStatus(
                confirmed_offset=total_bytes,
                completed=True,
                video_id=completed.video_id,
                call=completed.call,
            )
        if response.status_code in {capabilities.GONE_STATUS, capabilities.NOT_FOUND_STATUS}:
            return UploadStatus(
                confirmed_offset=start,
                completed=False,
                expired=True,
                call=ProviderCall(
                    operation=operation,
                    http_status=response.status_code,
                    provider_request_id=_request_id(response),
                ),
            )
        raise _fail(response, operation)

    async def cancel_resumable_upload(
        self, *, access_token: SecretValue, upload_uri: SecretValue
    ) -> ProviderCall:
        response = await self._client.delete(
            upload_uri.reveal(),
            headers={**self._auth(access_token), "Content-Length": "0"},
        )
        return ProviderCall(
            operation="videos.insert.cancel",
            http_status=response.status_code,
            provider_request_id=_request_id(response),
        )

    # -- video ---------------------------------------------------------------
    async def _video_resource(
        self, access_token: SecretValue, video_id: str, operation: str
    ) -> tuple[dict[str, Any], httpx.Response, int]:
        units = capabilities.CURRENT_QUOTA_PROFILE.cost("videos.list")
        response = await self._client.get(
            capabilities.VIDEOS_URL,
            params={"part": ",".join(capabilities.VIDEO_READ_PARTS), "id": video_id},
            headers=self._auth(access_token),
        )
        if response.status_code >= 400:
            raise _fail(response, operation, quota_units=units)
        items = response.json().get("items") or []
        if not items:
            raise provider_error(
                http_status=capabilities.NOT_FOUND_STATUS,
                operation=operation,
                summary="YouTube no longer returns this video for the connected channel",
                provider_request_id=_request_id(response),
                quota_units=units,
            )
        return items[0], response, units

    @staticmethod
    def _snapshot(item: dict[str, Any], response: httpx.Response, units: int) -> VideoSnapshot:
        status = item.get("status") or {}
        snippet = item.get("snippet") or {}
        publish_at = status.get("publishAt")
        parsed: datetime | None = None
        if isinstance(publish_at, str) and publish_at:
            try:
                parsed = datetime.fromisoformat(publish_at.replace("Z", "+00:00"))
            except ValueError:
                parsed = None
        return VideoSnapshot(
            video_id=str(item.get("id", "")),
            privacy_status=str(status.get("privacyStatus", "")),
            upload_status=str(status.get("uploadStatus", "")),
            made_for_kids=status.get("madeForKids"),
            contains_synthetic_media=status.get("containsSyntheticMedia"),
            embeddable=status.get("embeddable"),
            publish_at=parsed,
            title=str(snippet.get("title", ""))[:255],
            failure_reason=str(status.get("failureReason", ""))[:128],
            rejection_reason=str(status.get("rejectionReason", ""))[:128],
            call=ProviderCall(
                operation="videos.list",
                http_status=response.status_code,
                provider_request_id=_request_id(response),
                quota_units=units,
            ),
        )

    async def fetch_video(self, *, access_token: SecretValue, video_id: str) -> VideoSnapshot:
        item, response, units = await self._video_resource(access_token, video_id, "videos.list")
        return self._snapshot(item, response, units)

    async def fetch_processing_status(
        self, *, access_token: SecretValue, video_id: str
    ) -> ProcessingSnapshot:
        item, response, units = await self._video_resource(access_token, video_id, "videos.list")
        details = item.get("processingDetails") or {}
        progress = details.get("processingProgress") or {}
        status = item.get("status") or {}

        def _as_int(value: object) -> int | None:
            try:
                return int(str(value))
            except (TypeError, ValueError):
                return None

        return ProcessingSnapshot(
            video_id=str(item.get("id", "")),
            processing_status=str(details.get("processingStatus", "")),
            parts_total=_as_int(progress.get("partsTotal")),
            parts_processed=_as_int(progress.get("partsProcessed")),
            failure_reason=str(details.get("processingFailureReason", ""))[:128]
            or str(status.get("failureReason", ""))[:128],
            upload_status=str(status.get("uploadStatus", "")),
            call=ProviderCall(
                operation="videos.list",
                http_status=response.status_code,
                provider_request_id=_request_id(response),
                quota_units=units,
            ),
        )

    async def _update(
        self, access_token: SecretValue, body: dict[str, Any], operation: str
    ) -> VideoSnapshot:
        units = capabilities.CURRENT_QUOTA_PROFILE.cost("videos.update")
        response = await self._client.put(
            capabilities.VIDEOS_URL,
            params={"part": ",".join(capabilities.VIDEO_UPDATE_PARTS)},
            json=body,
            headers={**self._auth(access_token), "Content-Type": "application/json; charset=UTF-8"},
        )
        if response.status_code >= 400:
            raise _fail(response, operation, quota_units=units)
        payload = response.json()
        item = payload if isinstance(payload, dict) else {}
        return self._snapshot(item, response, units)

    async def update_metadata(
        self, *, access_token: SecretValue, video_id: str, metadata: VideoMetadata
    ) -> VideoSnapshot:
        body = {"id": video_id, **_snippet_and_status(metadata)}
        return await self._update(access_token, body, "videos.update")

    async def update_visibility(
        self, *, access_token: SecretValue, video_id: str, metadata: VideoMetadata
    ) -> VideoSnapshot:
        # The complete status part. `videos.update` replaces the part it is
        # given, so sending `privacyStatus` on its own would delete the
        # synthetic-media disclosure, the made-for-kids declaration and the
        # embeddable setting at the moment the video becomes visible.
        status = _snippet_and_status(metadata)["status"]
        units = capabilities.CURRENT_QUOTA_PROFILE.cost("videos.update")
        response = await self._client.put(
            capabilities.VIDEOS_URL,
            params={
                "part": "status",
                "notifySubscribers": str(metadata.notify_subscribers).lower(),
            },
            json={"id": video_id, "status": status},
            headers={**self._auth(access_token), "Content-Type": "application/json; charset=UTF-8"},
        )
        if response.status_code >= 400:
            raise _fail(response, "videos.update", quota_units=units)
        payload = response.json()
        return self._snapshot(payload if isinstance(payload, dict) else {}, response, units)

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
        body = {"snippet": {"videoId": video_id, "language": language, "name": name}}
        response = await self._client.post(
            capabilities.CAPTIONS_INSERT_URL,
            # Deliberately no `sync` parameter: automatic caption
            # synchronisation is deprecated, and VidGen ships a timed track.
            params={"uploadType": "multipart", "part": ",".join(capabilities.CAPTION_INSERT_PARTS)},
            files={
                "metadata": (None, json.dumps(body), "application/json; charset=UTF-8"),
                "media": ("captions.srt", content, media_type),
            },
            headers=self._auth(access_token),
        )
        if response.status_code >= 400:
            raise _fail(response, "captions.insert", quota_units=units)
        payload = response.json()
        snippet = (payload or {}).get("snippet") or {}
        return CaptionTrack(
            caption_id=str((payload or {}).get("id", "")),
            language=str(snippet.get("language", language)),
            name=str(snippet.get("name", name))[:150],
            is_draft=bool(snippet.get("isDraft", False)),
            track_kind=str(snippet.get("trackKind", "standard")),
            call=ProviderCall(
                operation="captions.insert",
                http_status=response.status_code,
                provider_request_id=_request_id(response),
                quota_units=units,
            ),
        )

    async def list_captions(
        self, *, access_token: SecretValue, video_id: str
    ) -> tuple[CaptionTrack, ...]:
        units = capabilities.CURRENT_QUOTA_PROFILE.cost("captions.list")
        response = await self._client.get(
            capabilities.CAPTIONS_URL,
            params={"part": ",".join(capabilities.CAPTION_LIST_PARTS), "videoId": video_id},
            headers=self._auth(access_token),
        )
        if response.status_code >= 400:
            raise _fail(response, "captions.list", quota_units=units)
        tracks: list[CaptionTrack] = []
        for item in response.json().get("items") or []:
            snippet = item.get("snippet") or {}
            tracks.append(
                CaptionTrack(
                    caption_id=str(item.get("id", "")),
                    language=str(snippet.get("language", "")),
                    name=str(snippet.get("name", ""))[:150],
                    is_draft=bool(snippet.get("isDraft", False)),
                    track_kind=str(snippet.get("trackKind", "standard")),
                    call=ProviderCall(
                        operation="captions.list",
                        http_status=response.status_code,
                        quota_units=units,
                    ),
                )
            )
        return tuple(tracks)

    async def set_thumbnail(
        self, *, access_token: SecretValue, video_id: str, content: bytes, media_type: str
    ) -> ThumbnailResult:
        units = capabilities.CURRENT_QUOTA_PROFILE.cost("thumbnails.set")
        response = await self._client.post(
            capabilities.THUMBNAILS_SET_URL,
            params={"videoId": video_id, "uploadType": "media"},
            content=content,
            headers={**self._auth(access_token), "Content-Type": media_type},
        )
        if response.status_code >= 400:
            raise _fail(response, "thumbnails.set", quota_units=units)
        items = (response.json() or {}).get("items") or []
        default = (items[0].get("default") if items and isinstance(items[0], dict) else {}) or {}
        url = str(default.get("url", ""))
        return ThumbnailResult(
            video_id=video_id,
            url=url if url.startswith("https://") else "",
            width=default.get("width"),
            height=default.get("height"),
            call=ProviderCall(
                operation="thumbnails.set",
                http_status=response.status_code,
                provider_request_id=_request_id(response),
                quota_units=units,
            ),
        )
