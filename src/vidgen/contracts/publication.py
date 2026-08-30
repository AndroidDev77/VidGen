"""Strict, versioned T25 YouTube publication contracts.

T25 takes an approved, current, T22-``PASS`` render and publishes it to the
connected user's own YouTube channel. Four rules shape everything here:

* **No credential ever crosses a contract boundary.** There is no field for an
  access token, a refresh token, an authorization code, a PKCE verifier, a
  client secret or a resumable session URI. Those live encrypted in the
  publisher tables and are read only by the publisher worker and the backend
  OAuth handler. What a contract carries instead is a *fingerprint*: a hash, a
  version, an expiry, a status.
* **The provider is projected, never passed through.** A YouTube response
  becomes a bounded :class:`PublicationProviderResult`; no Google client-library
  object, no raw payload and no arbitrary JSON reaches the pipeline, the API,
  the dashboard or Temporal history.
* **Offsets are facts, not guesses.** A :class:`ResumableUploadCheckpoint`
  records the offset *YouTube confirmed*, is never greater than the total size,
  and is what a resumed upload restarts from.
* **Ambiguity is a state, not an assumption.** When the pipeline cannot prove
  whether a video was created, it says so with
  :class:`PublicationStatus.HUMAN_REVIEW_REQUIRED` and preserves the evidence
  rather than uploading again.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from vidgen.contracts.common import StrictContract

CONTRACT_VERSION = "publication/1.0"

Sha256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
ByteOffset = Annotated[int, Field(ge=0)]
#: A YouTube resource ID. Bounded and character-restricted so a projection can
#: never smuggle a URL, a query string or a token through an ID field.
ProviderId = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_\-]+$")]
BoundedText = Annotated[str, Field(max_length=500)]


class PublicationStatus(StrEnum):
    """The publication state machine.

    The happy path is strictly ordered. Every other member is terminal or a
    documented waiting state that a human or an explicit command leaves.
    """

    DRAFT = "DRAFT"
    AUTHORIZATION_REQUIRED = "AUTHORIZATION_REQUIRED"
    READY = "READY"
    UPLOAD_INITIALIZING = "UPLOAD_INITIALIZING"
    UPLOADING = "UPLOADING"
    PROCESSING = "PROCESSING"
    UPLOADING_CAPTIONS = "UPLOADING_CAPTIONS"
    UPLOADING_THUMBNAIL = "UPLOADING_THUMBNAIL"
    PRIVATE_READY = "PRIVATE_READY"
    VISIBILITY_UPDATING = "VISIBILITY_UPDATING"
    PUBLISHED = "PUBLISHED"
    # -- waiting and terminal states --
    REAUTHORIZATION_REQUIRED = "REAUTHORIZATION_REQUIRED"
    QUOTA_BLOCKED = "QUOTA_BLOCKED"
    PROCESSING_FAILED = "PROCESSING_FAILED"
    HUMAN_REVIEW_REQUIRED = "HUMAN_REVIEW_REQUIRED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


#: The forward progression. A transition is legal when the target is in the
#: source's set; the same set is compiled into a database CHECK constraint by
#: :mod:`vidgen.db.publication_models`, so application code and the database
#: agree on what a publication may do next.
ALLOWED_TRANSITIONS: dict[PublicationStatus, frozenset[PublicationStatus]] = {
    PublicationStatus.DRAFT: frozenset(
        {
            PublicationStatus.AUTHORIZATION_REQUIRED,
            PublicationStatus.READY,
            PublicationStatus.CANCELLED,
            PublicationStatus.FAILED,
        }
    ),
    PublicationStatus.AUTHORIZATION_REQUIRED: frozenset(
        {
            PublicationStatus.READY,
            PublicationStatus.DRAFT,
            PublicationStatus.CANCELLED,
            PublicationStatus.FAILED,
        }
    ),
    PublicationStatus.READY: frozenset(
        {
            PublicationStatus.UPLOAD_INITIALIZING,
            PublicationStatus.DRAFT,
            PublicationStatus.AUTHORIZATION_REQUIRED,
            PublicationStatus.REAUTHORIZATION_REQUIRED,
            PublicationStatus.QUOTA_BLOCKED,
            PublicationStatus.CANCELLED,
            PublicationStatus.FAILED,
        }
    ),
    PublicationStatus.UPLOAD_INITIALIZING: frozenset(
        {
            PublicationStatus.UPLOADING,
            PublicationStatus.READY,
            PublicationStatus.REAUTHORIZATION_REQUIRED,
            PublicationStatus.QUOTA_BLOCKED,
            PublicationStatus.HUMAN_REVIEW_REQUIRED,
            PublicationStatus.CANCELLED,
            PublicationStatus.FAILED,
        }
    ),
    PublicationStatus.UPLOADING: frozenset(
        {
            PublicationStatus.PROCESSING,
            PublicationStatus.UPLOADING,
            PublicationStatus.UPLOAD_INITIALIZING,
            PublicationStatus.REAUTHORIZATION_REQUIRED,
            PublicationStatus.QUOTA_BLOCKED,
            PublicationStatus.HUMAN_REVIEW_REQUIRED,
            PublicationStatus.CANCELLED,
            PublicationStatus.FAILED,
        }
    ),
    PublicationStatus.PROCESSING: frozenset(
        {
            PublicationStatus.UPLOADING_CAPTIONS,
            PublicationStatus.UPLOADING_THUMBNAIL,
            PublicationStatus.PRIVATE_READY,
            PublicationStatus.PROCESSING,
            PublicationStatus.PROCESSING_FAILED,
            PublicationStatus.QUOTA_BLOCKED,
            PublicationStatus.REAUTHORIZATION_REQUIRED,
            PublicationStatus.HUMAN_REVIEW_REQUIRED,
            PublicationStatus.FAILED,
        }
    ),
    # A caption or thumbnail failure never returns to an upload state: the
    # video already exists and must never be uploaded a second time.
    PublicationStatus.UPLOADING_CAPTIONS: frozenset(
        {
            PublicationStatus.UPLOADING_THUMBNAIL,
            PublicationStatus.PRIVATE_READY,
            PublicationStatus.UPLOADING_CAPTIONS,
            PublicationStatus.QUOTA_BLOCKED,
            PublicationStatus.REAUTHORIZATION_REQUIRED,
            PublicationStatus.HUMAN_REVIEW_REQUIRED,
            PublicationStatus.FAILED,
        }
    ),
    PublicationStatus.UPLOADING_THUMBNAIL: frozenset(
        {
            PublicationStatus.PRIVATE_READY,
            PublicationStatus.UPLOADING_THUMBNAIL,
            PublicationStatus.QUOTA_BLOCKED,
            PublicationStatus.REAUTHORIZATION_REQUIRED,
            PublicationStatus.HUMAN_REVIEW_REQUIRED,
            PublicationStatus.FAILED,
        }
    ),
    PublicationStatus.PRIVATE_READY: frozenset(
        {
            PublicationStatus.VISIBILITY_UPDATING,
            PublicationStatus.UPLOADING_CAPTIONS,
            PublicationStatus.UPLOADING_THUMBNAIL,
            PublicationStatus.PUBLISHED,
            PublicationStatus.QUOTA_BLOCKED,
            PublicationStatus.REAUTHORIZATION_REQUIRED,
            PublicationStatus.HUMAN_REVIEW_REQUIRED,
            PublicationStatus.FAILED,
        }
    ),
    PublicationStatus.VISIBILITY_UPDATING: frozenset(
        {
            PublicationStatus.PUBLISHED,
            PublicationStatus.PRIVATE_READY,
            PublicationStatus.QUOTA_BLOCKED,
            PublicationStatus.REAUTHORIZATION_REQUIRED,
            PublicationStatus.HUMAN_REVIEW_REQUIRED,
            PublicationStatus.FAILED,
        }
    ),
    # A published video may still have its visibility changed again.
    PublicationStatus.PUBLISHED: frozenset(
        {PublicationStatus.VISIBILITY_UPDATING, PublicationStatus.HUMAN_REVIEW_REQUIRED}
    ),
    PublicationStatus.REAUTHORIZATION_REQUIRED: frozenset(
        {
            PublicationStatus.READY,
            PublicationStatus.UPLOADING,
            PublicationStatus.PROCESSING,
            PublicationStatus.UPLOADING_CAPTIONS,
            PublicationStatus.UPLOADING_THUMBNAIL,
            PublicationStatus.PRIVATE_READY,
            PublicationStatus.CANCELLED,
            PublicationStatus.FAILED,
        }
    ),
    PublicationStatus.QUOTA_BLOCKED: frozenset(
        {
            PublicationStatus.READY,
            PublicationStatus.UPLOADING,
            PublicationStatus.PROCESSING,
            PublicationStatus.UPLOADING_CAPTIONS,
            PublicationStatus.UPLOADING_THUMBNAIL,
            PublicationStatus.PRIVATE_READY,
            PublicationStatus.VISIBILITY_UPDATING,
            PublicationStatus.CANCELLED,
            PublicationStatus.FAILED,
        }
    ),
    PublicationStatus.PROCESSING_FAILED: frozenset(
        {PublicationStatus.HUMAN_REVIEW_REQUIRED, PublicationStatus.FAILED}
    ),
    # Only a human decision leaves review, and only towards a terminal state or
    # back to the private video that already exists.
    PublicationStatus.HUMAN_REVIEW_REQUIRED: frozenset(
        {
            PublicationStatus.PRIVATE_READY,
            PublicationStatus.UPLOADING_CAPTIONS,
            PublicationStatus.UPLOADING_THUMBNAIL,
            PublicationStatus.CANCELLED,
            PublicationStatus.FAILED,
        }
    ),
    PublicationStatus.FAILED: frozenset(),
    PublicationStatus.CANCELLED: frozenset(),
}

#: States from which no further work is scheduled.
TERMINAL_STATUSES: frozenset[PublicationStatus] = frozenset(
    {PublicationStatus.FAILED, PublicationStatus.CANCELLED}
)

#: States in which a YouTube video ID must already be persisted.
VIDEO_ID_REQUIRED_STATUSES: frozenset[PublicationStatus] = frozenset(
    {
        PublicationStatus.PROCESSING,
        PublicationStatus.UPLOADING_CAPTIONS,
        PublicationStatus.UPLOADING_THUMBNAIL,
        PublicationStatus.PRIVATE_READY,
        PublicationStatus.VISIBILITY_UPDATING,
        PublicationStatus.PUBLISHED,
        PublicationStatus.PROCESSING_FAILED,
    }
)


def transition_allowed(source: PublicationStatus, target: PublicationStatus) -> bool:
    """Whether the state machine permits ``source -> target``."""
    if source is target:
        return target in ALLOWED_TRANSITIONS.get(source, frozenset())
    return target in ALLOWED_TRANSITIONS.get(source, frozenset())


class PublicationPhase(StrEnum):
    """The restartable phase a publication run is currently executing."""

    ELIGIBILITY = "ELIGIBILITY"
    AUTHORIZATION = "AUTHORIZATION"
    UPLOAD_INITIALIZATION = "UPLOAD_INITIALIZATION"
    MEDIA_UPLOAD = "MEDIA_UPLOAD"
    PROCESSING_POLL = "PROCESSING_POLL"
    CAPTIONS = "CAPTIONS"
    THUMBNAIL = "THUMBNAIL"
    VERIFICATION = "VERIFICATION"
    VISIBILITY = "VISIBILITY"
    FINALIZATION = "FINALIZATION"


class PrivacyState(StrEnum):
    """The privacy states YouTube accepts. Mirrors the capability registry."""

    PRIVATE = "private"
    UNLISTED = "unlisted"
    PUBLIC = "public"


class ProcessingState(StrEnum):
    """A normalized processing outcome, independent of provider spelling."""

    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


class PublicationAssetKind(StrEnum):
    VIDEO = "video"
    CAPTION = "caption"
    THUMBNAIL = "thumbnail"


class PublicationAssetStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SUCCEEDED = "succeeded"
    SKIPPED = "skipped"
    FAILED = "failed"


class ConnectionStatus(StrEnum):
    CONNECTED = "connected"
    REAUTHORIZATION_REQUIRED = "reauthorization_required"
    REVOKED = "revoked"
    DISCONNECTED = "disconnected"


class PublicationFailureCode(StrEnum):
    """The T23 failure classification for every YouTube operation.

    Every member maps to exactly one operator action, which is why there is no
    generic ``ERROR``.
    """

    # -- eligibility, decided before any provider call --
    NO_ELIGIBLE_RENDER = "NO_ELIGIBLE_RENDER"
    RENDER_NOT_SELECTED = "RENDER_NOT_SELECTED"
    RENDER_NOT_APPROVED = "RENDER_NOT_APPROVED"
    COMPLETION_GATE_NOT_PASSED = "COMPLETION_GATE_NOT_PASSED"
    STALE_RENDER = "STALE_RENDER"
    UNRESOLVED_HUMAN_REVIEW = "UNRESOLVED_HUMAN_REVIEW"
    CROSS_PROJECT_REFERENCE = "CROSS_PROJECT_REFERENCE"
    MISSING_FINAL_ASSET = "MISSING_FINAL_ASSET"
    MISSING_CAPTION_ASSET = "MISSING_CAPTION_ASSET"
    INVALID_THUMBNAIL_ASSET = "INVALID_THUMBNAIL_ASSET"
    INVALID_METADATA = "INVALID_METADATA"
    INVALID_SCHEDULE = "INVALID_SCHEDULE"
    CONNECTION_NOT_OWNED = "CONNECTION_NOT_OWNED"
    # -- authorization --
    AUTHENTICATION_REQUIRED = "AUTHENTICATION_REQUIRED"
    INVALID_GRANT = "INVALID_GRANT"
    INSUFFICIENT_SCOPE = "INSUFFICIENT_SCOPE"
    CHANNEL_MISMATCH = "CHANNEL_MISMATCH"
    # -- provider --
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    UPLOAD_LIMIT_EXCEEDED = "UPLOAD_LIMIT_EXCEEDED"
    RATE_LIMITED = "RATE_LIMITED"
    RETRYABLE_SERVER_ERROR = "RETRYABLE_SERVER_ERROR"
    EXPIRED_RESUMABLE_SESSION = "EXPIRED_RESUMABLE_SESSION"
    AMBIGUOUS_COMPLETION = "AMBIGUOUS_COMPLETION"
    PROCESSING_FAILED = "PROCESSING_FAILED"
    CAPTION_CONFLICT = "CAPTION_CONFLICT"
    THUMBNAIL_NOT_PERMITTED = "THUMBNAIL_NOT_PERMITTED"
    PRIVACY_RESTRICTED = "PRIVACY_RESTRICTED"
    PROVIDER_REJECTED = "PROVIDER_REJECTED"
    CANCELLED = "CANCELLED"


#: Failure codes a retry can plausibly clear without human intervention.
RETRYABLE_FAILURE_CODES: frozenset[PublicationFailureCode] = frozenset(
    {
        PublicationFailureCode.RATE_LIMITED,
        PublicationFailureCode.RETRYABLE_SERVER_ERROR,
        PublicationFailureCode.QUOTA_EXCEEDED,
        PublicationFailureCode.UPLOAD_LIMIT_EXCEEDED,
    }
)


class PublicationFailure(StrictContract):
    """A structured, renderable publication failure. Never carries a secret."""

    schema_version: Literal["1.0"] = "1.0"
    code: PublicationFailureCode
    summary: BoundedText
    retryable: bool = False
    #: The HTTP status YouTube returned, when the failure came from a request.
    http_status: int | None = Field(default=None, ge=100, le=599)
    #: Google's structured ``reason``, when one was present.
    provider_reason: str = Field(default="", max_length=128)
    #: The local row a caller can inspect: a run, an asset, a session.
    reference_id: UUID | None = None
    #: What the user or operator should do next. Actionable, never a traceback.
    remediation: BoundedText = ""
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class PublicationWarning(StrictContract):
    """A non-blocking observation surfaced to the dashboard and the CLI."""

    schema_version: Literal["1.0"] = "1.0"
    code: str = Field(min_length=1, max_length=64)
    summary: BoundedText
    reference_id: UUID | None = None


# -- OAuth and connections ----------------------------------------------------


class YouTubeChannel(StrictContract):
    """A channel identity resolved from YouTube, never supplied by a browser."""

    schema_version: Literal["1.0"] = "1.0"
    channel_id: ProviderId
    title: str = Field(min_length=1, max_length=255)
    #: Only an ``https`` URL on a YouTube-controlled host is persisted.
    thumbnail_url: str = Field(default="", max_length=1000)
    custom_url: str = Field(default="", max_length=255)
    #: Whether the channel may set a custom thumbnail, when YouTube tells us.
    supports_custom_thumbnails: bool | None = None

    @field_validator("thumbnail_url")
    @classmethod
    def only_https(cls, value: str) -> str:
        if value and not value.startswith("https://"):
            raise ValueError("a channel thumbnail URL must be https")
        return value


class YouTubeConnection(StrictContract):
    """One owner's connected channel. Carries no token, only their fingerprint."""

    schema_version: Literal["1.0"] = "1.0"
    connection_id: UUID
    owner_subject: str = Field(min_length=1, max_length=255)
    channel: YouTubeChannel
    status: ConnectionStatus
    granted_scopes: list[str] = Field(default_factory=list, max_length=16)
    #: When the *access* token expires. The refresh token's own lifetime is not
    #: knowable, which is why ``INVALID_GRANT`` is a first-class failure.
    credential_expires_at: datetime | None = None
    #: The envelope-encryption key version the ciphertext was sealed with, so a
    #: rotation is auditable without decrypting anything.
    encryption_key_version: str = Field(default="", max_length=64)
    last_verified_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def connected_requires_scopes(self) -> YouTubeConnection:
        if self.status is ConnectionStatus.CONNECTED and not self.granted_scopes:
            raise ValueError("a connected channel must record the scopes it was granted")
        return self


class YouTubeOAuthState(StrictContract):
    """A one-time authorization attempt. The verifier never appears here.

    Only the *hash* of the ``state`` parameter is persisted and projected: a
    leaked projection therefore cannot be replayed against the callback.
    """

    schema_version: Literal["1.0"] = "1.0"
    state_id: UUID
    state_hash: Sha256
    owner_subject: str = Field(min_length=1, max_length=255)
    #: Where the browser is sent after a successful callback. Always validated
    #: against the configured allowlist before it is stored *and* before it is
    #: used, so a widened allowlist cannot retroactively bless an old row.
    redirect_target: str = Field(default="", max_length=1000)
    #: The exact redirect URI registered with Google and sent in both the
    #: authorization request and the token exchange.
    redirect_uri: str = Field(min_length=1, max_length=1000)
    requested_scopes: list[str] = Field(min_length=1, max_length=16)
    code_challenge_method: Literal["S256"] = "S256"
    expires_at: datetime
    consumed_at: datetime | None = None
    created_at: datetime

    @model_validator(mode="after")
    def expiry_after_creation(self) -> YouTubeOAuthState:
        if self.expires_at <= self.created_at:
            raise ValueError("an OAuth state must expire after it was created")
        return self


class OAuthAuthorizationRequest(StrictContract):
    """What the API hands the browser to start an authorization."""

    schema_version: Literal["1.0"] = "1.0"
    state_id: UUID
    #: The Google authorization URL. It carries the public client ID, the
    #: scopes, the redirect URI, the state and the PKCE *challenge* - never the
    #: verifier and never a secret.
    authorization_url: str = Field(min_length=1, max_length=2000)
    expires_at: datetime

    @field_validator("authorization_url")
    @classmethod
    def only_the_official_endpoint(cls, value: str) -> str:
        if not value.startswith("https://accounts.google.com/"):
            raise ValueError("the authorization URL must be Google's official endpoint")
        return value


# -- draft and metadata -------------------------------------------------------


class PublicationMetadata(StrictContract):
    """The editable YouTube metadata for one publication, at one version.

    Limits come from the capability registry and are enforced twice: here, and
    again by the provider request builder immediately before a write.
    """

    schema_version: Literal["1.0"] = "1.0"
    metadata_version: int = Field(default=1, ge=1)
    title: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=5000)
    tags: list[str] = Field(default_factory=list, max_length=60)
    category_id: str = Field(default="24", min_length=1, max_length=8, pattern=r"^[0-9]+$")
    default_language: str = Field(default="en", min_length=2, max_length=16)
    caption_language: str = Field(default="en", min_length=2, max_length=16)
    caption_track_name: str = Field(default="VidGen recap", min_length=1, max_length=150)
    made_for_kids: bool = False
    #: ``status.containsSyntheticMedia``. VidGen output is animated and AI
    #: generated, so this defaults to true and is always sent.
    contains_synthetic_media: bool = True
    embeddable: bool = True
    notify_subscribers: bool = False
    #: The privacy state every upload starts in. Constrained to ``private``:
    #: there is no way to express a public initial upload.
    initial_privacy: Literal[PrivacyState.PRIVATE] = PrivacyState.PRIVATE
    #: What the user has asked for *eventually*. Reaching it always requires an
    #: explicit later action.
    requested_privacy: PrivacyState = PrivacyState.PRIVATE
    #: ``status.publishAt``. UTC, and only meaningful for a scheduled private
    #: video.
    scheduled_publish_at: datetime | None = None

    @field_validator("title", "description")
    @classmethod
    def no_forbidden_characters(cls, value: str) -> str:
        if "<" in value or ">" in value:
            raise ValueError("angle brackets are rejected by YouTube in titles and descriptions")
        return value

    @field_validator("tags")
    @classmethod
    def bounded_tags(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for tag in value:
            stripped = tag.strip()
            if not stripped:
                continue
            if len(stripped) > 30:
                raise ValueError("a single tag may not exceed 30 characters")
            cleaned.append(stripped)
        if sum(len(tag) for tag in cleaned) > 500:
            raise ValueError("the tag list may not exceed 500 characters in total")
        return cleaned

    @field_validator("scheduled_publish_at")
    @classmethod
    def schedule_is_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("a scheduled publish time must carry a UTC offset")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def schedule_requires_private_target(self) -> PublicationMetadata:
        if (
            self.scheduled_publish_at is not None
            and self.requested_privacy is not PrivacyState.PUBLIC
        ):
            raise ValueError("a scheduled publication must request the public privacy state")
        return self


class PublicationDraft(StrictContract):
    """The versioned, user-editable draft a publication is started from.

    A draft is created deterministically from existing project metadata. It is
    never regenerated over a user's edits: the pipeline reads the persisted
    draft, and a reload or a resume reads the same row.
    """

    schema_version: Literal["1.0"] = "1.0"
    draft_id: UUID
    project_id: UUID
    final_render_asset_id: UUID
    final_editorial_run_id: UUID
    connection_id: UUID
    channel_id: ProviderId
    metadata: PublicationMetadata
    thumbnail_asset_id: UUID | None = None
    #: The hash of everything the draft was derived from, so a change in the
    #: project is visible without diffing text.
    input_hash: Sha256
    created_at: datetime
    updated_at: datetime


# -- eligibility --------------------------------------------------------------


class PublicationGate(StrictContract):
    """Whether this project may publish, and exactly why not when it may not.

    Recomputed before starting an upload *and* again immediately before a
    visibility change, so a render selected in between can never be published
    under an older render's approval.
    """

    schema_version: Literal["1.0"] = "1.0"
    project_id: UUID
    allowed: bool
    final_render_asset_id: UUID | None = None
    render_job_id: UUID | None = None
    render_identity: Sha256 | None = None
    final_editorial_run_id: UUID | None = None
    completion_gate_id: UUID | None = None
    approval_id: UUID | None = None
    caption_asset_id: UUID | None = None
    thumbnail_asset_id: UUID | None = None
    gate_version: str = Field(default="", max_length=64)
    failures: list[PublicationFailure] = Field(default_factory=list, max_length=32)
    warnings: list[PublicationWarning] = Field(default_factory=list, max_length=32)
    evaluated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def allowed_is_complete(self) -> PublicationGate:
        if self.allowed:
            if self.failures:
                raise ValueError("an allowed gate may not carry a failure")
            missing = [
                name
                for name, value in (
                    ("final_render_asset_id", self.final_render_asset_id),
                    ("final_editorial_run_id", self.final_editorial_run_id),
                    ("approval_id", self.approval_id),
                    ("caption_asset_id", self.caption_asset_id),
                )
                if value is None
            ]
            if missing:
                raise ValueError(f"an allowed gate must name {', '.join(missing)}")
        elif not self.failures:
            raise ValueError("a refused gate must explain itself")
        return self


# -- provider boundary --------------------------------------------------------


class PublicationProviderRequest(StrictContract):
    """One provider-neutral request. Carries no credential and no media bytes."""

    schema_version: Literal["1.0"] = "1.0"
    operation: str = Field(min_length=1, max_length=64)
    publication_run_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=255)
    #: The hash of the canonical request body, so a retry proves it is the same
    #: request rather than asserting it.
    input_hash: Sha256
    capability_profile_version: str = Field(min_length=1, max_length=64)
    #: Bounded, non-secret request attributes: an offset, a language, a count.
    attributes: dict[str, str | int | bool] = Field(default_factory=dict)

    @field_validator("attributes")
    @classmethod
    def bounded_attributes(cls, value: dict[str, str | int | bool]) -> dict[str, str | int | bool]:
        if len(value) > 24:
            raise ValueError("a provider request may carry at most 24 attributes")
        for key, item in value.items():
            if len(key) > 64:
                raise ValueError("provider request attribute names are bounded")
            if isinstance(item, str) and len(item) > 255:
                raise ValueError("provider request attribute values are bounded")
        return value


class PublicationProviderResult(StrictContract):
    """A bounded projection of one YouTube response.

    The raw payload is discarded at the adapter boundary. What survives is the
    handful of fields the pipeline and the dashboard actually use.
    """

    schema_version: Literal["1.0"] = "1.0"
    operation: str = Field(min_length=1, max_length=64)
    succeeded: bool
    http_status: int | None = Field(default=None, ge=100, le=599)
    #: Google's opaque request identifier, for a support conversation.
    provider_request_id: str = Field(default="", max_length=255)
    #: Any resource ID the call produced: a video ID, a caption track ID.
    resource_id: str = Field(default="", max_length=128)
    quota_units: int = Field(default=0, ge=0)
    latency_ms: int = Field(default=0, ge=0)
    retry_count: int = Field(default=0, ge=0)
    #: Bounded scalar projection of the response. Never a raw payload.
    projection: dict[str, str | int | bool | None] = Field(default_factory=dict)
    failure: PublicationFailure | None = None

    @model_validator(mode="after")
    def failure_matches_outcome(self) -> PublicationProviderResult:
        if self.succeeded and self.failure is not None:
            raise ValueError("a successful provider result may not carry a failure")
        if not self.succeeded and self.failure is None:
            raise ValueError("a failed provider result must carry its classification")
        return self

    @field_validator("projection")
    @classmethod
    def bounded_projection(
        cls, value: dict[str, str | int | bool | None]
    ) -> dict[str, str | int | bool | None]:
        if len(value) > 32:
            raise ValueError("a provider projection is bounded to 32 keys")
        for key, item in value.items():
            if len(key) > 64:
                raise ValueError("provider projection keys are bounded")
            if isinstance(item, str) and len(item) > 500:
                raise ValueError("provider projection values are bounded")
        return value


class ResumableUploadCheckpoint(StrictContract):
    """The durable state of one resumable upload session.

    The session URI itself is a bearer credential: it is encrypted at rest and
    is deliberately absent from this contract. What is projected is its
    *identity hash*, which is enough to prove two checkpoints refer to the same
    session without being enough to upload to it.
    """

    schema_version: Literal["1.0"] = "1.0"
    session_id: UUID
    publication_run_id: UUID
    #: SHA-256 of the session URI. Recovery evidence, not a credential.
    session_uri_hash: Sha256
    encryption_key_version: str = Field(min_length=1, max_length=64)
    total_bytes: int = Field(gt=0)
    #: The offset YouTube itself confirmed. Never the local optimistic count.
    confirmed_offset: ByteOffset = 0
    chunk_bytes: int = Field(gt=0)
    status: Literal["active", "completed", "expired", "ambiguous", "cancelled"] = "active"
    last_response_code: int | None = Field(default=None, ge=100, le=599)
    video_id: ProviderId | None = None
    provider_attempt_id: UUID | None = None
    expires_at: datetime | None = None
    last_confirmed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def offset_within_total(self) -> ResumableUploadCheckpoint:
        if self.confirmed_offset > self.total_bytes:
            raise ValueError("a confirmed offset may never exceed the total size")
        if self.status == "completed":
            if self.confirmed_offset != self.total_bytes:
                raise ValueError("a completed session must have confirmed every byte")
            if not self.video_id:
                raise ValueError("a completed session must carry the YouTube video ID")
        return self

    @property
    def remaining_bytes(self) -> int:
        return self.total_bytes - self.confirmed_offset


class PublicationAssetResult(StrictContract):
    """One uploaded YouTube sub-resource: the video, a caption or a thumbnail."""

    schema_version: Literal["1.0"] = "1.0"
    publication_asset_id: UUID
    publication_run_id: UUID
    kind: PublicationAssetKind
    status: PublicationAssetStatus
    local_asset_id: UUID | None = None
    local_asset_sha256: Sha256 | None = None
    #: The YouTube ID this local asset became. For a thumbnail there is none:
    #: ``thumbnails.set`` returns a resource without an ID of its own.
    provider_resource_id: str = Field(default="", max_length=128)
    provider_attempt_id: UUID | None = None
    byte_size: int = Field(default=0, ge=0)
    language: str = Field(default="", max_length=16)
    name: str = Field(default="", max_length=150)
    failure: PublicationFailure | None = None
    projection: dict[str, str | int | bool | None] = Field(default_factory=dict)
    created_at: datetime
    completed_at: datetime | None = None

    @model_validator(mode="after")
    def succeeded_is_complete(self) -> PublicationAssetResult:
        if self.status is PublicationAssetStatus.SUCCEEDED:
            if self.completed_at is None:
                raise ValueError("a succeeded asset must record when it completed")
            if self.kind is not PublicationAssetKind.THUMBNAIL and not self.provider_resource_id:
                raise ValueError("a succeeded video or caption must carry its YouTube ID")
        if self.status is PublicationAssetStatus.FAILED and self.failure is None:
            raise ValueError("a failed asset must carry its classification")
        return self


class PublicationAttempt(StrictContract):
    """One instrumented YouTube operation, projected for the dashboard and CLI."""

    schema_version: Literal["1.0"] = "1.0"
    attempt_id: UUID
    publication_run_id: UUID
    operation: str = Field(min_length=1, max_length=64)
    attempt_number: int = Field(ge=1)
    #: The T23 ``provider_attempts`` row this projects. T25 never duplicates
    #: that table; it references it.
    provider_attempt_id: UUID | None = None
    provider: str = Field(min_length=1, max_length=64)
    status: Literal["started", "succeeded", "failed"] = "started"
    http_status: int | None = Field(default=None, ge=100, le=599)
    provider_request_id: str = Field(default="", max_length=255)
    latency_ms: int = Field(default=0, ge=0)
    retry_count: int = Field(default=0, ge=0)
    bytes_uploaded: int = Field(default=0, ge=0)
    confirmed_offset: ByteOffset = 0
    #: Quota units consumed. A rate-limit quantity, never a monetary charge.
    quota_units: int = Field(default=0, ge=0)
    failure: PublicationFailure | None = None
    started_at: datetime
    completed_at: datetime | None = None


class PublicationProgress(StrictContract):
    """The live, bounded progress projection the dashboard polls."""

    schema_version: Literal["1.0"] = "1.0"
    publication_run_id: UUID
    status: PublicationStatus
    phase: PublicationPhase
    total_bytes: int = Field(default=0, ge=0)
    confirmed_offset: ByteOffset = 0
    chunk_bytes: int = Field(default=0, ge=0)
    processing_state: ProcessingState | None = None
    #: YouTube's own ``processingProgress.partsProcessed`` ratio, when present.
    processing_percent: float | None = Field(default=None, ge=0, le=100)
    caption_status: PublicationAssetStatus | None = None
    thumbnail_status: PublicationAssetStatus | None = None
    quota_units: int = Field(default=0, ge=0)
    updated_at: datetime

    @model_validator(mode="after")
    def offset_within_total(self) -> PublicationProgress:
        if self.total_bytes and self.confirmed_offset > self.total_bytes:
            raise ValueError("a confirmed offset may never exceed the total size")
        return self


class PublicationResult(StrictContract):
    """The compact projection returned by the pipeline, API, CLI and activity.

    Deliberately free of every credential-shaped field. The dashboard renders
    this verbatim.
    """

    schema_version: Literal["1.0"] = "1.0"
    publication_run_id: UUID
    project_id: UUID
    connection_id: UUID
    channel_id: ProviderId
    final_render_asset_id: UUID
    final_editorial_run_id: UUID
    approval_id: UUID | None = None
    publication_identity: Sha256
    idempotency_key: str = Field(min_length=1, max_length=255)
    metadata_version: int = Field(default=1, ge=1)
    status: PublicationStatus
    phase: PublicationPhase
    video_id: ProviderId | None = None
    video_url: str = Field(default="", max_length=255)
    total_bytes: int = Field(default=0, ge=0)
    confirmed_offset: ByteOffset = 0
    processing_state: ProcessingState | None = None
    caption_status: PublicationAssetStatus | None = None
    caption_track_id: str = Field(default="", max_length=128)
    thumbnail_status: PublicationAssetStatus | None = None
    requested_privacy: PrivacyState = PrivacyState.PRIVATE
    #: What YouTube actually reports. Never inferred from the request.
    actual_privacy: PrivacyState | None = None
    scheduled_publish_at: datetime | None = None
    contains_synthetic_media: bool = True
    made_for_kids: bool = False
    notify_subscribers: bool = False
    quota_units: int = Field(default=0, ge=0)
    capability_profile_version: str = Field(default="", max_length=64)
    publisher_version: str = Field(default="", max_length=64)
    failure: PublicationFailure | None = None
    warnings: list[PublicationWarning] = Field(default_factory=list, max_length=32)
    reused: bool = False
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def state_invariants(self) -> PublicationResult:
        if self.status in VIDEO_ID_REQUIRED_STATUSES and not self.video_id:
            raise ValueError(f"{self.status.value} requires a persisted YouTube video ID")
        if self.confirmed_offset > self.total_bytes and self.total_bytes:
            raise ValueError("a confirmed offset may never exceed the total size")
        if self.status is PublicationStatus.PUBLISHED and self.actual_privacy is None:
            raise ValueError("a published result must record the privacy YouTube reports")
        return self


class PublicationActivityInput(StrictContract):
    """The compact ID-only message a Temporal activity receives.

    No metadata text, no credential, no session URI, no caption or thumbnail
    bytes and no provider payload: everything else is loaded from the database
    by the worker inside the activity.
    """

    schema_version: Literal["1.0"] = "1.0"
    project_id: UUID
    publication_run_id: UUID
    connection_id: UUID
    final_render_asset_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=255)
    trace_context: dict[str, str] = Field(default_factory=dict)

    @field_validator("trace_context")
    @classmethod
    def bounded_trace(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 8:
            raise ValueError("the trace context is bounded to 8 entries")
        return value


class PublicationActivityResult(StrictContract):
    """The compact ID-only projection an activity returns to the workflow."""

    schema_version: Literal["1.0"] = "1.0"
    publication_run_id: UUID
    status: PublicationStatus
    phase: PublicationPhase
    video_id: ProviderId | None = None
    confirmed_offset: ByteOffset = 0
    total_bytes: int = Field(default=0, ge=0)
    processing_state: ProcessingState | None = None
    failure_code: PublicationFailureCode | None = None
    retryable: bool = False
