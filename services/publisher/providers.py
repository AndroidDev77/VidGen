"""Provider selection, failure classification and bounded transport retries.

The classification table is the single place an HTTP status or a Google error
reason becomes a :class:`~vidgen.contracts.publication.PublicationFailureCode`.
Keeping it here rather than at each call site is what makes the T23 failure
taxonomy consistent across the adapter, the pipeline and the dashboard, and what
lets the fake provider produce exactly the same classifications as production.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from uuid import UUID

from services.publisher import youtube as capabilities
from services.publisher.contracts import YouTubeProvider, YouTubeProviderError
from vidgen.contracts.publication import PublicationFailure, PublicationFailureCode

#: Google error reasons that outrank the bare HTTP status they arrive with.
_REASON_CODES: dict[str, PublicationFailureCode] = {
    capabilities.REASON_QUOTA_EXCEEDED: PublicationFailureCode.QUOTA_EXCEEDED,
    capabilities.REASON_UPLOAD_LIMIT_EXCEEDED: PublicationFailureCode.UPLOAD_LIMIT_EXCEEDED,
    capabilities.REASON_RATE_LIMIT_EXCEEDED: PublicationFailureCode.RATE_LIMITED,
    capabilities.REASON_USER_RATE_LIMIT_EXCEEDED: PublicationFailureCode.RATE_LIMITED,
    capabilities.REASON_CAPTION_EXISTS: PublicationFailureCode.CAPTION_CONFLICT,
    capabilities.REASON_INSUFFICIENT_PERMISSIONS: PublicationFailureCode.INSUFFICIENT_SCOPE,
    capabilities.REASON_PRIVACY_RESTRICTED: PublicationFailureCode.PRIVACY_RESTRICTED,
    capabilities.REASON_INVALID_GRANT: PublicationFailureCode.INVALID_GRANT,
}

#: What a user or operator should actually do, per classification.
REMEDIATION: dict[PublicationFailureCode, str] = {
    PublicationFailureCode.AUTHENTICATION_REQUIRED: (
        "Connect a YouTube channel before publishing."
    ),
    PublicationFailureCode.INVALID_GRANT: (
        "The YouTube authorization was revoked or expired. Reconnect the channel."
    ),
    PublicationFailureCode.INSUFFICIENT_SCOPE: (
        "Reconnect the channel: this connection was granted fewer permissions than "
        "uploading a video with captions and a thumbnail requires."
    ),
    PublicationFailureCode.QUOTA_EXCEEDED: (
        "The channel's daily YouTube API quota is exhausted. Publication resumes "
        "after the quota resets; no bytes are re-uploaded."
    ),
    PublicationFailureCode.UPLOAD_LIMIT_EXCEEDED: (
        "The channel has reached its daily upload limit. Retry after it resets."
    ),
    PublicationFailureCode.RATE_LIMITED: "YouTube is rate limiting this channel. Retrying.",
    PublicationFailureCode.RETRYABLE_SERVER_ERROR: "YouTube returned a server error. Retrying.",
    PublicationFailureCode.EXPIRED_RESUMABLE_SESSION: (
        "The resumable upload session expired. The publication is held for review so "
        "no second copy of the video can be created."
    ),
    PublicationFailureCode.AMBIGUOUS_COMPLETION: (
        "YouTube may or may not have created the video. Check the channel's uploads "
        "and resolve the publication before retrying."
    ),
    PublicationFailureCode.PROCESSING_FAILED: (
        "YouTube could not process the uploaded video. The video ID is retained for "
        "investigation; re-encode the render before publishing again."
    ),
    PublicationFailureCode.CAPTION_CONFLICT: (
        "A caption track with this language and name already exists on the video. "
        "The existing track is reused rather than duplicated."
    ),
    PublicationFailureCode.THUMBNAIL_NOT_PERMITTED: (
        "This channel cannot set custom thumbnails. The video stays private with "
        "YouTube's generated thumbnail; verify the channel before retrying."
    ),
    PublicationFailureCode.PRIVACY_RESTRICTED: (
        "This API project may only upload private videos until it is verified by "
        "Google. The video remains private."
    ),
}


def classify(
    *, http_status: int | None, reason: str = "", operation: str = ""
) -> tuple[PublicationFailureCode, bool]:
    """Classify one failed YouTube response into a code and its retryability."""
    normalized = (reason or "").strip()
    if normalized in _REASON_CODES:
        code = _REASON_CODES[normalized]
        return code, code in {
            PublicationFailureCode.RATE_LIMITED,
            PublicationFailureCode.QUOTA_EXCEEDED,
            PublicationFailureCode.UPLOAD_LIMIT_EXCEEDED,
        }
    if http_status is None:
        return PublicationFailureCode.RETRYABLE_SERVER_ERROR, True
    if http_status == capabilities.UNAUTHORIZED_STATUS:
        return PublicationFailureCode.AUTHENTICATION_REQUIRED, False
    if http_status == capabilities.FORBIDDEN_STATUS:
        # 403 without a reason is ambiguous between scope and permission. The
        # operation disambiguates: only thumbnails.set has a channel-capability
        # form of 403 that must keep the private video intact.
        if operation == "thumbnails.set":
            return PublicationFailureCode.THUMBNAIL_NOT_PERMITTED, False
        return PublicationFailureCode.INSUFFICIENT_SCOPE, False
    if http_status == capabilities.CONFLICT_STATUS:
        return PublicationFailureCode.CAPTION_CONFLICT, False
    if http_status in {
        capabilities.GONE_STATUS,
        capabilities.NOT_FOUND_STATUS,
    } and operation.startswith("videos.insert"):
        return PublicationFailureCode.EXPIRED_RESUMABLE_SESSION, False
    if http_status in capabilities.RETRYABLE_STATUS_CODES:
        if http_status == 429:
            return PublicationFailureCode.RATE_LIMITED, True
        return PublicationFailureCode.RETRYABLE_SERVER_ERROR, True
    return PublicationFailureCode.PROVIDER_REJECTED, False


def provider_error(
    *,
    http_status: int | None,
    reason: str = "",
    operation: str = "",
    summary: str = "",
    provider_request_id: str = "",
    quota_units: int = 0,
) -> YouTubeProviderError:
    """Build a classified provider error with its remediation attached."""
    code, retryable = classify(http_status=http_status, reason=reason, operation=operation)
    message = summary or f"{operation or 'youtube'} failed with status {http_status}"
    return YouTubeProviderError(
        code,
        message,
        http_status=http_status,
        reason=reason,
        retryable=retryable,
        remediation=REMEDIATION.get(code, ""),
        provider_request_id=provider_request_id,
        quota_units=quota_units,
    )


def failure_from(error: YouTubeProviderError, *, reference_id: object = None) -> PublicationFailure:
    """Project a provider error into the persisted, renderable failure contract."""
    return PublicationFailure(
        code=error.code,
        summary=str(error)[:500],
        retryable=error.retryable,
        http_status=error.http_status,
        provider_reason=error.reason[:128],
        reference_id=reference_id if isinstance(reference_id, UUID) else None,
        remediation=(error.remediation or REMEDIATION.get(error.code, ""))[:500],
    )


async def with_transport_retries[T](
    operation: str,
    call: Callable[[], Awaitable[T]],
    *,
    max_attempts: int = capabilities.MAX_TRANSPORT_ATTEMPTS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    jitter: Callable[[], float] = random.random,
) -> tuple[T, int]:
    """Run ``call`` with bounded exponential backoff on retryable failures.

    Returns the result and the number of retries spent, so the retry count lands
    on the T23 provider attempt rather than being lost. A non-retryable
    classification is raised immediately: re-sending a request YouTube has
    already refused on its merits only burns quota.
    """
    delay = capabilities.TRANSPORT_BACKOFF_INITIAL_SECONDS
    retries = 0
    while True:
        try:
            return await call(), retries
        except YouTubeProviderError as error:
            if not error.retryable or retries + 1 >= max_attempts:
                raise
            retries += 1
            # Full jitter: a fleet of workers retrying a rate limit must not
            # re-converge on the same instant.
            await sleep(min(delay, capabilities.TRANSPORT_BACKOFF_MAX_SECONDS) * jitter())
            delay *= 2
        except asyncio.CancelledError:
            raise


#: The provider names a caller may select.
FAKE_PROVIDER = "fake"
YOUTUBE_PROVIDER = "youtube"
SUPPORTED_PROVIDERS = (FAKE_PROVIDER, YOUTUBE_PROVIDER)


class PublisherConfigurationError(RuntimeError):
    """The requested provider cannot be built from the current configuration."""


def build_provider(
    name: str,
    *,
    client_id: str | None = None,
    client_secret: str | None = None,
    fake_state: object | None = None,
) -> YouTubeProvider:
    """Construct the requested provider.

    The production adapter is imported lazily so a fake-provider run never needs
    an HTTP client configured, and so an unconfigured environment fails with a
    named missing setting rather than an import error.
    """
    normalized = name.strip().lower()
    if normalized == FAKE_PROVIDER:
        from services.publisher.fake_youtube import (
            FakeYouTubeProvider,
            FakeYouTubeState,
            shared_state,
        )

        # Without an explicit state, every fake provider in this process shares
        # one world: an API request and a worker running side by side must see
        # the same fake channel and the same fake videos.
        state = fake_state if isinstance(fake_state, FakeYouTubeState) else shared_state()
        return FakeYouTubeProvider(state)
    if normalized != YOUTUBE_PROVIDER:
        raise PublisherConfigurationError(
            f"unsupported publication provider {name!r}; expected one of {SUPPORTED_PROVIDERS}"
        )
    if not client_id:
        raise PublisherConfigurationError(
            "VIDGEN_YOUTUBE_OAUTH_CLIENT_ID is required for the production YouTube provider"
        )
    if not client_secret:
        raise PublisherConfigurationError(
            "VIDGEN_YOUTUBE_OAUTH_CLIENT_SECRET is required for the production YouTube provider"
        )
    from services.publisher.youtube_adapter import YouTubeDataApiProvider

    return YouTubeDataApiProvider(client_id=client_id, client_secret=client_secret)
