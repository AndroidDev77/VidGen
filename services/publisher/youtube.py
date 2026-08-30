"""The single official YouTube Data API v3 capability registry.

Every URL, OAuth endpoint, scope, size limit, privacy state, quota unit cost,
retryable status code and processing state the publisher depends on is declared
here once. Nothing else in ``services/publisher`` may hard-code any of them: a
capability profile is a *version*, it is persisted alongside every publication,
and a later profile therefore never silently reinterprets an already published
video.

Verified against the official YouTube Data API v3 reference documentation on the
date recorded in :data:`CAPABILITY_VERIFIED_ON`. Re-verify before changing a
value here, and bump :data:`CAPABILITY_PROFILE_VERSION` when anything a
publication binds into its identity changes.

Sources consulted for this profile:

* Videos: insert / update, and the ``status`` resource, including
  ``privacyStatus``, ``publishAt``, ``selfDeclaredMadeForKids`` and
  ``containsSyntheticMedia`` (added 2024-10-30).
* Captions: insert, including the deprecated ``sync`` parameter, which this
  repository never sends.
* Thumbnails: set.
* The resumable upload protocol, ``Content-Range`` and ``308 Resume
  Incomplete``.
* The quota-cost reference. Note the December 2025 reduction of
  ``videos.insert`` and the June 2026 move of uploads into a separate daily
  upload bucket: both profiles below are kept so an environment pinned to the
  older accounting is still describable.

Quota units are **not** money. They are recorded as a typed usage quantity by
:mod:`services.publisher.pipeline` with a zero monetary cost, because Google
does not bill for them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Final

#: The date the values in this module were last checked against the official
#: documentation. Persisted with every publication so a later profile change is
#: attributable.
CAPABILITY_VERIFIED_ON: Final = "2026-08-29"

#: Bumped whenever a field, limit or scope that a publication identity binds
#: changes. Persisted on ``publication_runs`` and on every provider attempt.
CAPABILITY_PROFILE_VERSION: Final = "youtube-data-v3/2026-08"

#: The publisher implementation version, bound into the publication identity so
#: a behavioural change forces a new identity rather than reusing an old row.
PUBLISHER_VERSION: Final = "t25/1.0"

# -- endpoints ----------------------------------------------------------------

API_BASE_URL: Final = "https://www.googleapis.com/youtube/v3"
UPLOAD_BASE_URL: Final = "https://www.googleapis.com/upload/youtube/v3"
OAUTH_AUTHORIZATION_URL: Final = "https://accounts.google.com/o/oauth2/v2/auth"
OAUTH_TOKEN_URL: Final = "https://oauth2.googleapis.com/token"
OAUTH_REVOCATION_URL: Final = "https://oauth2.googleapis.com/revoke"

VIDEOS_INSERT_URL: Final = f"{UPLOAD_BASE_URL}/videos"
VIDEOS_URL: Final = f"{API_BASE_URL}/videos"
CAPTIONS_INSERT_URL: Final = f"{UPLOAD_BASE_URL}/captions"
CAPTIONS_URL: Final = f"{API_BASE_URL}/captions"
THUMBNAILS_SET_URL: Final = f"{UPLOAD_BASE_URL}/thumbnails/set"
CHANNELS_URL: Final = f"{API_BASE_URL}/channels"

#: The video watch URL template. Only ever built from a video ID YouTube
#: returned; never from client input.
WATCH_URL_TEMPLATE: Final = "https://www.youtube.com/watch?v={video_id}"

# -- scopes -------------------------------------------------------------------

#: Uploading media. Sufficient for ``videos.insert`` and ``thumbnails.set``,
#: and *not* sufficient for ``captions.insert``.
SCOPE_UPLOAD: Final = "https://www.googleapis.com/auth/youtube.upload"
#: Required by every ``captions`` write, and by ``videos.update``. This is the
#: scope that makes the caption track this pipeline uploads possible at all.
SCOPE_FORCE_SSL: Final = "https://www.googleapis.com/auth/youtube.force-ssl"
#: Read-only channel identity verification after authorization.
SCOPE_READONLY: Final = "https://www.googleapis.com/auth/youtube.readonly"

#: The narrowest verified set that supports video upload, caption insertion,
#: thumbnail upload, and metadata and visibility updates. Deliberately excludes
#: every YouTube Partner and CMS scope: T25 manages one connected personal
#: channel, never a content-owner account.
REQUIRED_SCOPES: Final[tuple[str, ...]] = (
    SCOPE_UPLOAD,
    SCOPE_FORCE_SSL,
    SCOPE_READONLY,
)

#: Scopes that must never be requested. Asserted in tests so a later change
#: cannot quietly widen the grant into partner territory.
FORBIDDEN_SCOPE_FRAGMENTS: Final[tuple[str, ...]] = (
    "youtubepartner",
    "yt-analytics",
    "youtube.channel-memberships",
)

#: Per-operation scope requirements, so a connection missing a scope fails with
#: ``INSUFFICIENT_SCOPE`` before a request is made.
OPERATION_SCOPES: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        "channels.list": (SCOPE_READONLY,),
        "videos.insert": (SCOPE_UPLOAD,),
        "videos.list": (SCOPE_READONLY,),
        "videos.update": (SCOPE_FORCE_SSL,),
        "captions.insert": (SCOPE_FORCE_SSL,),
        "captions.list": (SCOPE_FORCE_SSL,),
        "thumbnails.set": (SCOPE_UPLOAD,),
    }
)

# -- OAuth --------------------------------------------------------------------

#: Offline access is what returns a refresh token at all.
OAUTH_ACCESS_TYPE: Final = "offline"
#: Forces a refresh token even when the user has authorized before.
OAUTH_PROMPT: Final = "consent"
#: PKCE. S256 is supported by Google's authorization endpoint and is the only
#: challenge method this repository sends; ``plain`` is never used.
PKCE_CODE_CHALLENGE_METHOD: Final = "S256"
PKCE_VERIFIER_MIN_LENGTH: Final = 43
PKCE_VERIFIER_MAX_LENGTH: Final = 128
#: An authorization request that is not completed within this window is dead.
OAUTH_STATE_TTL_SECONDS: Final = 600
#: Refresh a little before the provider's own expiry so a long upload does not
#: cross it mid-chunk.
ACCESS_TOKEN_REFRESH_SKEW_SECONDS: Final = 120

# -- media limits -------------------------------------------------------------

#: YouTube's documented maximum upload size: 256 GB.
MAX_VIDEO_BYTES: Final = 256 * 1024 * 1024 * 1024
#: Accepted final-render media types. VidGen delivers MP4.
ACCEPTED_VIDEO_MEDIA_TYPES: Final[tuple[str, ...]] = ("video/mp4", "video/quicktime")
CANONICAL_VIDEO_MEDIA_TYPE: Final = "video/mp4"

#: Resumable chunks must be a multiple of 256 KiB, except for the final chunk.
RESUMABLE_CHUNK_GRANULARITY: Final = 256 * 1024
#: The default chunk size: large enough that a long upload is not dominated by
#: per-request overhead, small enough that an interruption loses little.
DEFAULT_CHUNK_BYTES: Final = 8 * 1024 * 1024
MIN_CHUNK_BYTES: Final = RESUMABLE_CHUNK_GRANULARITY
MAX_CHUNK_BYTES: Final = 256 * 1024 * 1024
#: A resumable session URI is documented as valid for about a week.
RESUMABLE_SESSION_TTL_SECONDS: Final = 7 * 24 * 60 * 60

#: Caption limits. SRT is the canonical delivery format for T25.
MAX_CAPTION_BYTES: Final = 100 * 1024 * 1024
ACCEPTED_CAPTION_MEDIA_TYPES: Final[tuple[str, ...]] = (
    "application/x-subrip",
    "text/vtt",
    "text/plain",
)
CANONICAL_CAPTION_MEDIA_TYPE: Final = "application/x-subrip"
CANONICAL_CAPTION_FORMAT: Final = "srt"
SUPPORTED_CAPTION_FORMATS: Final[tuple[str, ...]] = ("srt", "vtt")
MAX_CAPTION_NAME_LENGTH: Final = 150
#: The deprecated ``captions.insert`` automatic-synchronisation parameter. It is
#: named here only so the adapter contract test can assert it is never sent.
DEPRECATED_CAPTION_SYNC_PARAMETER: Final = "sync"

#: Thumbnail limits. 2 MiB, JPEG or PNG, 16:9 recommended at 1280x720.
MAX_THUMBNAIL_BYTES: Final = 2 * 1024 * 1024
ACCEPTED_THUMBNAIL_MEDIA_TYPES: Final[tuple[str, ...]] = ("image/jpeg", "image/png")
RECOMMENDED_THUMBNAIL_WIDTH: Final = 1280
RECOMMENDED_THUMBNAIL_HEIGHT: Final = 720
MIN_THUMBNAIL_WIDTH: Final = 640
RECOMMENDED_THUMBNAIL_ASPECT_RATIO: Final = 16 / 9
#: How far a thumbnail may deviate from 16:9 before it is reported as a warning.
THUMBNAIL_ASPECT_RATIO_TOLERANCE: Final = 0.02

# -- metadata limits ----------------------------------------------------------

MAX_TITLE_LENGTH: Final = 100
MAX_DESCRIPTION_LENGTH: Final = 5000
MAX_TAG_LENGTH: Final = 30
#: YouTube counts the tag list's total characters, not the number of tags.
MAX_TAGS_TOTAL_CHARACTERS: Final = 500
MAX_TAG_COUNT: Final = 60
#: Characters YouTube rejects outright in a title or description.
FORBIDDEN_METADATA_CHARACTERS: Final[tuple[str, ...]] = ("<", ">")
DEFAULT_CATEGORY_ID: Final = "24"  # Entertainment
DEFAULT_LANGUAGE: Final = "en"

#: The ``part`` values sent with each write, declared once.
VIDEO_INSERT_PARTS: Final[tuple[str, ...]] = ("snippet", "status")
VIDEO_UPDATE_PARTS: Final[tuple[str, ...]] = ("snippet", "status")
VIDEO_READ_PARTS: Final[tuple[str, ...]] = ("status", "processingDetails", "snippet")
CAPTION_INSERT_PARTS: Final[tuple[str, ...]] = ("snippet",)
CAPTION_LIST_PARTS: Final[tuple[str, ...]] = ("snippet",)
CHANNEL_READ_PARTS: Final[tuple[str, ...]] = ("id", "snippet")


class PrivacyStatus(StrEnum):
    """The privacy states ``status.privacyStatus`` accepts."""

    PRIVATE = "private"
    UNLISTED = "unlisted"
    PUBLIC = "public"


#: Every upload starts here. Never public, never unlisted.
INITIAL_PRIVACY_STATUS: Final = PrivacyStatus.PRIVATE
#: Subscribers are never notified unless the user explicitly asks.
DEFAULT_NOTIFY_SUBSCRIBERS: Final = False
#: Only a scheduled *private* video may carry ``publishAt``.
SCHEDULABLE_FROM_PRIVACY: Final = PrivacyStatus.PRIVATE
#: A scheduled publication must be at least this far in the future.
MIN_SCHEDULE_LEAD_SECONDS: Final = 15 * 60
#: ...and no further out than this, so a typo cannot park a video for a decade.
MAX_SCHEDULE_LEAD_SECONDS: Final = 365 * 24 * 60 * 60


class ProcessingStatus(StrEnum):
    """``processingDetails.processingStatus`` values."""

    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TERMINATED = "terminated"


class UploadStatus(StrEnum):
    """``status.uploadStatus`` values."""

    UPLOADED = "uploaded"
    PROCESSED = "processed"
    FAILED = "failed"
    REJECTED = "rejected"
    DELETED = "deleted"


#: Terminal processing outcomes: polling stops as soon as one is observed.
TERMINAL_PROCESSING_STATUSES: Final[frozenset[str]] = frozenset(
    {
        ProcessingStatus.SUCCEEDED.value,
        ProcessingStatus.FAILED.value,
        ProcessingStatus.TERMINATED.value,
    }
)

# -- polling and retries ------------------------------------------------------

PROCESSING_POLL_INITIAL_SECONDS: Final = 5.0
PROCESSING_POLL_MAX_SECONDS: Final = 120.0
PROCESSING_POLL_BACKOFF_FACTOR: Final = 2.0
#: Total wall-clock budget for processing. Exceeding it is not a failure of the
#: video: the publication waits, keeping the persisted video ID.
PROCESSING_MAX_ELAPSED_SECONDS: Final = 6 * 60 * 60

#: Status codes worth retrying with backoff. ``408`` and ``429`` are transport
#: and rate limiting; ``5xx`` are server-side.
RETRYABLE_STATUS_CODES: Final[frozenset[int]] = frozenset({408, 429, 500, 502, 503, 504})
#: The resumable-protocol "keep going" status. Not an error.
RESUME_INCOMPLETE_STATUS: Final = 308
#: A resumable session that no longer exists.
GONE_STATUS: Final = 410
NOT_FOUND_STATUS: Final = 404
CONFLICT_STATUS: Final = 409
FORBIDDEN_STATUS: Final = 403
UNAUTHORIZED_STATUS: Final = 401
MAX_TRANSPORT_ATTEMPTS: Final = 5
TRANSPORT_BACKOFF_INITIAL_SECONDS: Final = 1.0
TRANSPORT_BACKOFF_MAX_SECONDS: Final = 60.0

#: Google error reasons this repository maps to structured failures.
REASON_QUOTA_EXCEEDED: Final = "quotaExceeded"
REASON_UPLOAD_LIMIT_EXCEEDED: Final = "uploadLimitExceeded"
REASON_RATE_LIMIT_EXCEEDED: Final = "rateLimitExceeded"
REASON_USER_RATE_LIMIT_EXCEEDED: Final = "userRateLimitExceeded"
REASON_CAPTION_EXISTS: Final = "captionExists"
REASON_FORBIDDEN: Final = "forbidden"
REASON_INSUFFICIENT_PERMISSIONS: Final = "insufficientPermissions"
#: The channel is not permitted to set a custom thumbnail.
REASON_THUMBNAIL_FORBIDDEN: Final = "forbidden"
#: An API project that has not completed verification may only upload private
#: videos; a public or unlisted transition is refused with this reason.
REASON_PRIVACY_RESTRICTED: Final = "privacyRestricted"
REASON_INVALID_GRANT: Final = "invalid_grant"

# -- quota --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class QuotaProfile:
    """One versioned accounting of YouTube's daily quota units.

    Quota units are a rate limit, not a price. They are recorded as a typed
    usage quantity with a zero monetary cost.
    """

    name: str
    verified_on: str
    #: Units per operation, keyed by the operation names in
    #: :data:`OPERATION_SCOPES`.
    units: Mapping[str, int]
    #: Daily units in the shared pool.
    daily_units: int
    #: Separate daily upload allowance, when the project has one. ``None`` when
    #: uploads bill from the shared pool.
    daily_upload_calls: int | None = None
    notes: str = ""

    def cost(self, operation: str) -> int:
        return int(self.units.get(operation, 0))


#: The accounting in force for :data:`CAPABILITY_PROFILE_VERSION`. Google
#: reduced ``videos.insert`` from 1600 units on 2025-12-04 and moved uploads
#: into their own daily allowance on 2026-06-01.
CURRENT_QUOTA_PROFILE: Final = QuotaProfile(
    name="youtube-data-v3/2026-06",
    verified_on=CAPABILITY_VERIFIED_ON,
    units=MappingProxyType(
        {
            "channels.list": 1,
            "videos.list": 1,
            "videos.insert": 100,
            "videos.update": 50,
            "captions.insert": 400,
            "captions.list": 50,
            "thumbnails.set": 50,
        }
    ),
    daily_units=10_000,
    daily_upload_calls=100,
    notes="uploads bill to a separate daily call allowance",
)

#: The pre-2025-12-04 accounting, kept so an environment pinned to the older
#: profile is describable rather than silently mis-reported.
LEGACY_QUOTA_PROFILE: Final = QuotaProfile(
    name="youtube-data-v3/2024-10",
    verified_on="2025-11-01",
    units=MappingProxyType(
        {
            "channels.list": 1,
            "videos.list": 1,
            "videos.insert": 1600,
            "videos.update": 50,
            "captions.insert": 400,
            "captions.list": 50,
            "thumbnails.set": 50,
        }
    ),
    daily_units=10_000,
    daily_upload_calls=None,
    notes="uploads billed from the shared 10,000-unit pool",
)

QUOTA_PROFILES: Final[Mapping[str, QuotaProfile]] = MappingProxyType(
    {profile.name: profile for profile in (CURRENT_QUOTA_PROFILE, LEGACY_QUOTA_PROFILE)}
)

#: The unit in which quota consumption is recorded on a T23 provider attempt.
QUOTA_USAGE_UNIT: Final = "youtube_quota_unit"
#: The provider name recorded on every T23 provider attempt and asset row.
PROVIDER_NAME: Final = "youtube"
#: The deterministic fake's provider name, so a fake attempt is never mistaken
#: for a real one in the cost ledger.
FAKE_PROVIDER_NAME: Final = "fake-youtube"


@dataclass(frozen=True, slots=True)
class CapabilityProfile:
    """The complete, versioned capability set bound to one publication."""

    version: str = CAPABILITY_PROFILE_VERSION
    verified_on: str = CAPABILITY_VERIFIED_ON
    publisher_version: str = PUBLISHER_VERSION
    quota: QuotaProfile = CURRENT_QUOTA_PROFILE
    scopes: tuple[str, ...] = REQUIRED_SCOPES
    #: ``status.containsSyntheticMedia`` is supported by this profile, so the
    #: pipeline is forbidden from omitting it.
    supports_synthetic_media_disclosure: bool = True
    supports_scheduled_publication: bool = True
    supports_upload_cancellation: bool = True
    max_video_bytes: int = MAX_VIDEO_BYTES
    max_caption_bytes: int = MAX_CAPTION_BYTES
    max_thumbnail_bytes: int = MAX_THUMBNAIL_BYTES
    chunk_bytes: int = DEFAULT_CHUNK_BYTES
    privacy_states: tuple[str, ...] = field(
        default_factory=lambda: tuple(status.value for status in PrivacyStatus)
    )

    def quota_cost(self, operation: str) -> int:
        return self.quota.cost(operation)


DEFAULT_CAPABILITY_PROFILE: Final = CapabilityProfile()


def normalize_chunk_bytes(requested: int) -> int:
    """Round a requested chunk size to a legal resumable chunk size.

    YouTube requires every chunk but the last to be a multiple of 256 KiB, so a
    misconfigured value is corrected here rather than rejected at byte 0 of a
    multi-gigabyte upload.
    """
    if requested < MIN_CHUNK_BYTES:
        requested = MIN_CHUNK_BYTES
    if requested > MAX_CHUNK_BYTES:
        requested = MAX_CHUNK_BYTES
    return (requested // RESUMABLE_CHUNK_GRANULARITY) * RESUMABLE_CHUNK_GRANULARITY


def watch_url(video_id: str) -> str:
    return WATCH_URL_TEMPLATE.format(video_id=video_id)


def missing_scopes(granted: tuple[str, ...] | list[str], operation: str) -> tuple[str, ...]:
    """The scopes ``operation`` needs that this connection does not hold."""
    held = set(granted)
    return tuple(scope for scope in OPERATION_SCOPES.get(operation, ()) if scope not in held)
