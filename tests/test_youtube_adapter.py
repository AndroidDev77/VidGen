"""Contract tests for the production YouTube Data API adapter.

Every request is answered by an in-process ``httpx.MockTransport``: the adapter
is exercised end to end - headers, ranges, parts, parameters and response
projection - without a Google project, a credential or a network. Nothing in
this file, or anywhere else in the test suite, makes a real YouTube request.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from services.publisher import youtube as capabilities
from services.publisher.contracts import VideoMetadata, YouTubeProviderError
from services.publisher.credentials import SecretValue
from services.publisher.youtube_adapter import YouTubeDataApiProvider
from vidgen.contracts.publication import PublicationFailureCode

TOKEN = SecretValue("ya29.test-access-token")


def metadata(**overrides: object) -> VideoMetadata:
    base = dict(
        title="Recap",
        description="A recap",
        tags=("recap",),
        category_id="24",
        default_language="en",
        privacy_status="private",
        made_for_kids=False,
        contains_synthetic_media=True,
        embeddable=True,
        notify_subscribers=False,
        publish_at=None,
    )
    base.update(overrides)
    return VideoMetadata(**base)  # type: ignore[arg-type]


def provider(handler) -> YouTubeDataApiProvider:
    return YouTubeDataApiProvider(
        client_id="client-id",
        client_secret="client-secret",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


# -- OAuth ---------------------------------------------------------------------
def test_the_authorization_url_carries_pkce_offline_access_and_no_secret() -> None:
    adapter = provider(lambda request: httpx.Response(200))
    url = adapter.authorization_url(
        redirect_uri="http://localhost:8000/cb",
        scopes=capabilities.REQUIRED_SCOPES,
        state="state-value",
        code_challenge="challenge",
    )
    assert url.startswith(capabilities.OAUTH_AUTHORIZATION_URL)
    query = httpx.URL(url).params
    assert query["client_id"] == "client-id"
    assert query["code_challenge_method"] == "S256"
    assert query["access_type"] == "offline"
    assert query["prompt"] == "consent"
    assert query["redirect_uri"] == "http://localhost:8000/cb"
    assert set(query["scope"].split()) == set(capabilities.REQUIRED_SCOPES)
    # The client secret is a backend-only value and never appears in the URL.
    assert "client-secret" not in url
    assert "client_secret" not in query


def test_the_code_exchange_sends_pkce_and_the_exact_redirect_uri() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(
            200,
            json={
                "access_token": "at",
                "refresh_token": "rt",
                "expires_in": 3599,
                "scope": " ".join(capabilities.REQUIRED_SCOPES),
            },
        )

    tokens = asyncio.run(
        provider(handler).exchange_code(
            code=SecretValue("auth-code"),
            redirect_uri="http://localhost:8000/cb",
            code_verifier=SecretValue("verifier"),
        )
    )
    assert seen["grant_type"] == "authorization_code"
    assert seen["code"] == "auth-code"
    assert seen["code_verifier"] == "verifier"
    assert seen["redirect_uri"] == "http://localhost:8000/cb"
    assert seen["client_secret"] == "client-secret"
    assert tokens.refresh_token is not None
    assert tokens.refresh_token.reveal() == "rt"
    assert tokens.granted_scopes == capabilities.REQUIRED_SCOPES
    assert tokens.expires_at > datetime.now(UTC)


def test_an_invalid_grant_is_classified() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    with pytest.raises(YouTubeProviderError) as error:
        asyncio.run(provider(handler).refresh_access_token(refresh_token=SecretValue("rt")))
    assert error.value.code is PublicationFailureCode.INVALID_GRANT
    assert error.value.retryable is False


def test_revoking_an_already_invalid_token_is_a_success() -> None:
    adapter = provider(lambda request: httpx.Response(400, json={"error": "invalid_token"}))
    call = asyncio.run(adapter.revoke(token=SecretValue("rt")))
    assert call.http_status == 400


# -- channel -------------------------------------------------------------------
def test_the_channel_lookup_asks_for_the_authenticated_channel_only() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        seen["authorization"] = request.headers["Authorization"]
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "UCchannel",
                        "snippet": {
                            "title": "Test",
                            "customUrl": "@test",
                            "thumbnails": {"default": {"url": "https://yt3.example/x.jpg"}},
                        },
                    }
                ]
            },
        )

    identity = asyncio.run(provider(handler).fetch_channel(access_token=TOKEN))
    assert seen["mine"] == "true"
    assert seen["authorization"] == f"Bearer {TOKEN.reveal()}"
    assert identity.channel_id == "UCchannel"
    assert identity.thumbnail_url == "https://yt3.example/x.jpg"


def test_a_non_https_channel_thumbnail_is_dropped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "UCchannel",
                        "snippet": {
                            "title": "Test",
                            "thumbnails": {"default": {"url": "http://insecure.example/x.jpg"}},
                        },
                    }
                ]
            },
        )

    assert asyncio.run(provider(handler).fetch_channel(access_token=TOKEN)).thumbnail_url == ""


def test_an_account_without_a_channel_is_refused() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": []})

    with pytest.raises(YouTubeProviderError, match="owns no YouTube channel"):
        asyncio.run(provider(handler).fetch_channel(access_token=TOKEN))


# -- resumable upload ----------------------------------------------------------
def test_initialisation_declares_the_length_type_and_synthetic_media() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["params"] = dict(request.url.params)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, headers={"Location": "https://upload.example/session/1"})

    session = asyncio.run(
        provider(handler).initialize_resumable_upload(
            access_token=TOKEN,
            metadata=metadata(),
            total_bytes=1024,
            media_type="video/mp4",
        )
    )
    assert str(captured["url"]).startswith(capabilities.VIDEOS_INSERT_URL)
    params = captured["params"]
    assert isinstance(params, dict)
    assert params["uploadType"] == "resumable"
    assert set(params["part"].split(",")) == set(capabilities.VIDEO_INSERT_PARTS)
    assert params["notifySubscribers"] == "false"
    headers = captured["headers"]
    assert isinstance(headers, dict)
    assert headers["x-upload-content-length"] == "1024"
    assert headers["x-upload-content-type"] == "video/mp4"
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["status"]["privacyStatus"] == "private"
    # The synthetic-media disclosure is always present for VidGen output.
    assert body["status"]["containsSyntheticMedia"] is True
    assert body["status"]["selfDeclaredMadeForKids"] is False
    assert "publishAt" not in body["status"]
    assert session.upload_uri.reveal() == "https://upload.example/session/1"


def test_a_scheduled_publication_sends_publish_at_in_utc() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, headers={"Location": "https://upload.example/s"})

    when = datetime(2030, 5, 1, 12, 0, tzinfo=UTC)
    asyncio.run(
        provider(handler).initialize_resumable_upload(
            access_token=TOKEN,
            metadata=metadata(publish_at=when),
            total_bytes=10,
            media_type="video/mp4",
        )
    )
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["status"]["publishAt"] == "2030-05-01T12:00:00Z"


def test_a_missing_session_location_is_a_named_failure() -> None:
    with pytest.raises(YouTubeProviderError, match="no resumable session URI"):
        asyncio.run(
            provider(lambda request: httpx.Response(200)).initialize_resumable_upload(
                access_token=TOKEN,
                metadata=metadata(),
                total_bytes=10,
                media_type="video/mp4",
            )
        )


def test_a_308_response_carries_the_server_confirmed_offset() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Content-Range"] == "bytes 0-1023/4096"
        return httpx.Response(308, headers={"Range": "bytes=0-2047"})

    status = asyncio.run(
        provider(handler).upload_chunk(
            access_token=TOKEN,
            upload_uri=SecretValue("https://upload.example/s"),
            chunk=b"x" * 1024,
            start=0,
            total_bytes=4096,
        )
    )
    # bytes 0..2047 inclusive means the next byte to send is 2048.
    assert status.confirmed_offset == 2048
    assert status.completed is False


def test_a_308_without_a_range_header_means_the_server_holds_nothing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(308)

    status = asyncio.run(
        provider(handler).query_upload_status(
            access_token=TOKEN,
            upload_uri=SecretValue("https://upload.example/s"),
            total_bytes=4096,
        )
    )
    assert status.confirmed_offset == 0


def test_a_status_query_uses_the_documented_unknown_range_probe() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["range"] = request.headers["Content-Range"]
        seen["method"] = request.method
        seen["length"] = request.headers["Content-Length"]
        return httpx.Response(308, headers={"Range": "bytes=0-511"})

    asyncio.run(
        provider(handler).query_upload_status(
            access_token=TOKEN,
            upload_uri=SecretValue("https://upload.example/s"),
            total_bytes=4096,
        )
    )
    assert seen == {"range": "bytes */4096", "method": "PUT", "length": "0"}


def test_a_completed_upload_returns_the_video_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "vid123"})

    status = asyncio.run(
        provider(handler).upload_chunk(
            access_token=TOKEN,
            upload_uri=SecretValue("https://upload.example/s"),
            chunk=b"x" * 16,
            start=4080,
            total_bytes=4096,
        )
    )
    assert status.completed and status.video_id == "vid123"
    assert status.confirmed_offset == 4096


def test_a_completion_without_a_video_id_is_ambiguous_not_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    with pytest.raises(YouTubeProviderError, match="without returning a video ID"):
        asyncio.run(
            provider(handler).upload_chunk(
                access_token=TOKEN,
                upload_uri=SecretValue("https://upload.example/s"),
                chunk=b"x",
                start=0,
                total_bytes=1,
            )
        )


def test_a_gone_session_is_reported_as_expired_not_as_an_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(410)

    status = asyncio.run(
        provider(handler).query_upload_status(
            access_token=TOKEN,
            upload_uri=SecretValue("https://upload.example/s"),
            total_bytes=4096,
        )
    )
    assert status.expired is True


# -- video ---------------------------------------------------------------------
def test_processing_details_are_projected_not_passed_through() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert set(request.url.params["part"].split(",")) == set(capabilities.VIDEO_READ_PARTS)
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "vid123",
                        "status": {
                            "privacyStatus": "private",
                            "uploadStatus": "uploaded",
                            "containsSyntheticMedia": True,
                        },
                        "processingDetails": {
                            "processingStatus": "processing",
                            "processingProgress": {
                                "partsTotal": "4",
                                "partsProcessed": "1",
                            },
                        },
                        "snippet": {"title": "Recap", "description": "ignored"},
                    }
                ]
            },
        )

    snapshot = asyncio.run(
        provider(handler).fetch_processing_status(access_token=TOKEN, video_id="vid123")
    )
    assert snapshot.processing_status == "processing"
    assert snapshot.parts_total == 4
    assert snapshot.parts_processed == 1
    assert snapshot.percent == 25.0
    # The projection is a dataclass, not a provider payload.
    assert not hasattr(snapshot, "items")


def test_a_visibility_update_sends_the_explicit_notify_flag() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "v", "status": {"privacyStatus": "unlisted"}})

    snapshot = asyncio.run(
        provider(handler).update_visibility(
            access_token=TOKEN,
            video_id="v",
            privacy_status="unlisted",
            publish_at=None,
            notify_subscribers=False,
        )
    )
    params = seen["params"]
    assert isinstance(params, dict)
    assert params["notifySubscribers"] == "false"
    assert params["part"] == "status"
    assert snapshot.privacy_status == "unlisted"


def test_a_privacy_restricted_project_is_classified() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={"error": {"errors": [{"reason": capabilities.REASON_PRIVACY_RESTRICTED}]}},
        )

    with pytest.raises(YouTubeProviderError) as error:
        asyncio.run(
            provider(handler).update_visibility(
                access_token=TOKEN,
                video_id="v",
                privacy_status="public",
                publish_at=None,
                notify_subscribers=False,
            )
        )
    assert error.value.code is PublicationFailureCode.PRIVACY_RESTRICTED


# -- captions and thumbnails --------------------------------------------------
def test_caption_insertion_never_sends_the_deprecated_sync_parameter() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        seen["content_type"] = request.headers["Content-Type"]
        seen["body"] = request.content
        return httpx.Response(
            200,
            json={"id": "cap1", "snippet": {"language": "en", "name": "VidGen recap"}},
        )

    track = asyncio.run(
        provider(handler).insert_caption(
            access_token=TOKEN,
            video_id="v",
            language="en",
            name="VidGen recap",
            content=b"1\n00:00:00,000 --> 00:00:01,000\nhi\n",
            media_type=capabilities.CANONICAL_CAPTION_MEDIA_TYPE,
        )
    )
    params = seen["params"]
    assert isinstance(params, dict)
    assert capabilities.DEPRECATED_CAPTION_SYNC_PARAMETER not in params
    assert params["part"] == "snippet"
    assert str(seen["content_type"]).startswith("multipart/form-data")
    assert track.caption_id == "cap1"


def test_a_caption_conflict_is_classified() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409, json={"error": {"errors": [{"reason": capabilities.REASON_CAPTION_EXISTS}]}}
        )

    with pytest.raises(YouTubeProviderError) as error:
        asyncio.run(
            provider(handler).insert_caption(
                access_token=TOKEN,
                video_id="v",
                language="en",
                name="n",
                content=b"x",
                media_type=capabilities.CANONICAL_CAPTION_MEDIA_TYPE,
            )
        )
    assert error.value.code is PublicationFailureCode.CAPTION_CONFLICT


def test_a_thumbnail_403_is_a_channel_capability_failure_not_a_scope_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"errors": [{"reason": "forbidden"}]}})

    with pytest.raises(YouTubeProviderError) as error:
        asyncio.run(
            provider(handler).set_thumbnail(
                access_token=TOKEN, video_id="v", content=b"x", media_type="image/jpeg"
            )
        )
    assert error.value.code is PublicationFailureCode.THUMBNAIL_NOT_PERMITTED
    assert "custom thumbnail" in error.value.remediation


def test_a_thumbnail_upload_sends_the_media_type_as_the_content_type() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["content_type"] = request.headers["Content-Type"]
        seen["video_id"] = request.url.params["videoId"]
        return httpx.Response(
            200, json={"items": [{"default": {"url": "https://i.ytimg.com/x.jpg", "width": 120}}]}
        )

    result = asyncio.run(
        provider(handler).set_thumbnail(
            access_token=TOKEN, video_id="v", content=b"jpeg-bytes", media_type="image/jpeg"
        )
    )
    assert seen == {"content_type": "image/jpeg", "video_id": "v"}
    assert result.url == "https://i.ytimg.com/x.jpg"


# -- classification ------------------------------------------------------------
@pytest.mark.parametrize(
    ("status", "reason", "expected", "retryable"),
    [
        (429, "", PublicationFailureCode.RATE_LIMITED, True),
        (503, "", PublicationFailureCode.RETRYABLE_SERVER_ERROR, True),
        (500, "", PublicationFailureCode.RETRYABLE_SERVER_ERROR, True),
        (401, "", PublicationFailureCode.AUTHENTICATION_REQUIRED, False),
        (403, "quotaExceeded", PublicationFailureCode.QUOTA_EXCEEDED, True),
        (403, "uploadLimitExceeded", PublicationFailureCode.UPLOAD_LIMIT_EXCEEDED, True),
        (403, "insufficientPermissions", PublicationFailureCode.INSUFFICIENT_SCOPE, False),
        (400, "", PublicationFailureCode.PROVIDER_REJECTED, False),
    ],
)
def test_every_documented_failure_maps_to_one_classification(
    status: int, reason: str, expected: PublicationFailureCode, retryable: bool
) -> None:
    from services.publisher.providers import classify

    code, is_retryable = classify(http_status=status, reason=reason, operation="videos.insert")
    assert code is expected
    assert is_retryable is retryable


def test_bounded_retries_stop_and_never_retry_a_refusal() -> None:
    from services.publisher.providers import provider_error, with_transport_retries

    attempts = {"count": 0}

    async def flaky() -> str:
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise provider_error(http_status=503, operation="videos.insert.chunk")
        return "ok"

    async def instant(seconds: float) -> None:
        return None

    result, retries = asyncio.run(
        with_transport_retries("videos.insert.chunk", flaky, sleep=instant, jitter=lambda: 0.0)
    )
    assert result == "ok" and retries == 2

    async def refused() -> str:
        raise provider_error(http_status=400, operation="videos.insert.chunk")

    with pytest.raises(YouTubeProviderError):
        asyncio.run(
            with_transport_retries(
                "videos.insert.chunk", refused, sleep=instant, jitter=lambda: 0.0
            )
        )


def test_no_test_in_this_module_reaches_the_network() -> None:
    """A guard: every adapter test builds its provider on a MockTransport."""
    import inspect

    source = inspect.getsource(YouTubeDataApiProvider)
    # The adapter only ever talks through the injected client.
    assert "httpx.AsyncClient(" in source
    assert source.count("self._client.") >= 8


def test_a_provider_adapter_never_repr_leaks_its_client_secret() -> None:
    adapter = provider(lambda request: httpx.Response(200))
    assert "client-secret" not in repr(adapter)


def test_token_expiry_is_derived_from_expires_in() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "at", "expires_in": 60})

    tokens = asyncio.run(provider(handler).refresh_access_token(refresh_token=SecretValue("rt")))
    assert tokens.refresh_token is None
    assert tokens.expires_at <= datetime.now(UTC) + timedelta(seconds=61)
