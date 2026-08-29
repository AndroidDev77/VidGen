"""Restartable relational state for T25 YouTube publication.

Five tables, and every one of them exists because something must be true even
when the application is halfway through a restart:

* ``youtube_connections`` - one owner's connected channel. Canonical, and free
  of ciphertext.
* ``youtube_connection_secrets`` - the sealed refresh and access tokens, in a
  separate table so a projection, a join or an accidental ``SELECT *`` over the
  connection cannot return ciphertext. A connection may only be ``connected``
  while the matching secret row exists.
* ``youtube_oauth_states`` - one-time authorization attempts. Only the *hash* of
  the state parameter is stored, and the PKCE verifier is sealed.
* ``publication_runs`` - one publication per stable publication identity, with
  the state machine enforced by a transition CHECK generated from the same
  table the application uses.
* ``youtube_upload_sessions`` - the durable resumable-upload checkpoint, holding
  the sealed session URI and the *server-confirmed* offset.
* ``publication_assets`` - one row per uploaded YouTube sub-resource, with the
  uniqueness that makes a duplicate caption, thumbnail or video impossible.

T23 owns provider attempts, telemetry and the cost ledger; T17, T18 and T22 own
renders, approvals and the completion gate. Every reference here points at those
tables and none of them is duplicated.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from vidgen.contracts.publication import (
    ALLOWED_TRANSITIONS,
    VIDEO_ID_REQUIRED_STATUSES,
    ConnectionStatus,
    PrivacyState,
    ProcessingState,
    PublicationAssetKind,
    PublicationAssetStatus,
    PublicationPhase,
    PublicationStatus,
)
from vidgen.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


def _sql_set(values: Iterable[StrEnum]) -> str:
    """Render an iterable of enum members as a SQL ``IN`` list."""
    rendered = ",".join(f"'{item.value}'" for item in sorted(values, key=lambda item: item.value))
    return f"({rendered})"


PUBLICATION_STATUSES = _sql_set(PublicationStatus)
PUBLICATION_PHASES = _sql_set(PublicationPhase)
PRIVACY_STATES = _sql_set(PrivacyState)
PROCESSING_STATES = _sql_set(ProcessingState)
CONNECTION_STATUSES = _sql_set(ConnectionStatus)
ASSET_KINDS = _sql_set(PublicationAssetKind)
ASSET_STATUSES = _sql_set(PublicationAssetStatus)
VIDEO_REQUIRED_STATUSES = _sql_set(VIDEO_ID_REQUIRED_STATUSES)


def transition_check_expression() -> str:
    """The state machine, compiled from :data:`ALLOWED_TRANSITIONS`.

    Generated rather than written by hand so the database constraint and the
    application's transition table can never disagree: a change to one is a
    change to both, and the migration test asserts the rendered text matches.
    """
    clauses = ["previous_status IS NULL"]
    for source in sorted(ALLOWED_TRANSITIONS, key=lambda item: item.value):
        targets = ALLOWED_TRANSITIONS[source]
        if not targets:
            continue
        allowed = ",".join(f"'{target.value}'" for target in sorted(targets, key=lambda x: x.value))
        clauses.append(f"(previous_status = '{source.value}' AND status IN ({allowed}))")
    return " OR ".join(clauses)


class YouTubeConnection(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One owner's connected YouTube channel. Holds no ciphertext."""

    __tablename__ = "youtube_connections"
    owner_subject: Mapped[str] = mapped_column(String(255), index=True)
    channel_id: Mapped[str] = mapped_column(String(128))
    channel_title: Mapped[str] = mapped_column(String(255))
    channel_thumbnail_url: Mapped[str] = mapped_column(String(1000), default="")
    custom_url: Mapped[str] = mapped_column(String(255), default="")
    granted_scopes: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32), default=ConnectionStatus.CONNECTED.value)
    #: The envelope key version the secret row was sealed with. Denormalized so
    #: a rotation sweep can find stale rows without decrypting anything.
    encryption_key_version: Mapped[str] = mapped_column(String(64), default="")
    #: Denormalized presence flag, so "connected implies a stored credential" is
    #: a database constraint rather than a convention.
    credential_present: Mapped[bool] = mapped_column(Boolean, default=False)
    credential_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disconnected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(128))
    __table_args__ = (
        # One live connection per owner and channel. A reconnect updates this
        # row rather than accumulating duplicates, and one owner can never see
        # or address another owner's channel row.
        Index(
            "uq_youtube_connections_owner_channel",
            "owner_subject",
            "channel_id",
            unique=True,
            postgresql_where=text("status <> 'disconnected'"),
            sqlite_where=text("status <> 'disconnected'"),
        ),
        CheckConstraint(f"status IN {CONNECTION_STATUSES}", name="youtube_connection_status"),
        CheckConstraint("length(channel_id) > 0", name="youtube_connection_channel_id"),
        # A connected channel always has a sealed credential and a key version.
        CheckConstraint(
            "status <> 'connected' OR (credential_present AND length(encryption_key_version) > 0)",
            name="youtube_connection_connected_requires_credential",
        ),
        CheckConstraint(
            "channel_thumbnail_url = '' OR channel_thumbnail_url LIKE 'https://%'",
            name="youtube_connection_https_thumbnail",
        ),
    )


class YouTubeConnectionSecret(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """The sealed OAuth credentials for one connection.

    Deliberately a separate table with no non-key columns worth projecting: it
    is read only by the backend OAuth handler and the publisher worker, and it
    is never joined into an API response.
    """

    __tablename__ = "youtube_connection_secrets"
    connection_id: Mapped[UUID] = mapped_column(
        ForeignKey("youtube_connections.id", ondelete="CASCADE"), unique=True
    )
    refresh_token_ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    refresh_token_nonce: Mapped[bytes] = mapped_column(LargeBinary)
    access_token_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    access_token_nonce: Mapped[bytes | None] = mapped_column(LargeBinary)
    access_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    encryption_key_version: Mapped[str] = mapped_column(String(64))
    __table_args__ = (
        CheckConstraint(
            "length(refresh_token_ciphertext) > 0 AND length(refresh_token_nonce) = 12",
            name="youtube_connection_secret_refresh_sealed",
        ),
        CheckConstraint(
            "(access_token_ciphertext IS NULL) = (access_token_nonce IS NULL)",
            name="youtube_connection_secret_access_pairs",
        ),
        CheckConstraint(
            "length(encryption_key_version) > 0", name="youtube_connection_secret_key_version"
        ),
    )


class YouTubeOAuthState(UUIDPrimaryKeyMixin, Base):
    """One pending authorization attempt. Single use, owner bound, expiring."""

    __tablename__ = "youtube_oauth_states"
    #: SHA-256 of the ``state`` parameter. The parameter itself is never stored,
    #: so a database read cannot be replayed against the callback.
    state_hash: Mapped[str] = mapped_column(String(64), unique=True)
    owner_subject: Mapped[str] = mapped_column(String(255), index=True)
    code_verifier_ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    code_verifier_nonce: Mapped[bytes] = mapped_column(LargeBinary)
    encryption_key_version: Mapped[str] = mapped_column(String(64))
    redirect_uri: Mapped[str] = mapped_column(String(1000))
    redirect_target: Mapped[str] = mapped_column(String(1000), default="")
    requested_scopes: Mapped[list[str]] = mapped_column(JSON, default=list)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        CheckConstraint("length(state_hash) = 64", name="youtube_oauth_state_hash_length"),
        CheckConstraint("expires_at > created_at", name="youtube_oauth_state_expiry"),
        CheckConstraint(
            "length(code_verifier_ciphertext) > 0 AND length(code_verifier_nonce) = 12",
            name="youtube_oauth_state_verifier_sealed",
        ),
        CheckConstraint(
            "consumed_at IS NULL OR consumed_at >= created_at",
            name="youtube_oauth_state_consumed_after_creation",
        ),
        Index("ix_youtube_oauth_states_expiry", "expires_at"),
    )


class PublicationRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One restartable publication of one render to one channel."""

    __tablename__ = "publication_runs"
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    final_render_asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT")
    )
    render_job_id: Mapped[UUID] = mapped_column(ForeignKey("render_jobs.id", ondelete="RESTRICT"))
    final_editorial_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("final_editorial_runs.id", ondelete="RESTRICT")
    )
    completion_gate_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("final_completion_gates.id", ondelete="RESTRICT")
    )
    approval_id: Mapped[UUID] = mapped_column(
        ForeignKey("render_approvals.id", ondelete="RESTRICT")
    )
    connection_id: Mapped[UUID] = mapped_column(
        ForeignKey("youtube_connections.id", ondelete="RESTRICT"), index=True
    )
    channel_id: Mapped[str] = mapped_column(String(128))
    owner_subject: Mapped[str] = mapped_column(String(255), index=True)
    #: The stable identity binding render, gate, approval, connection, metadata,
    #: caption, thumbnail, initial privacy, publisher and capability versions.
    publication_identity: Mapped[str] = mapped_column(String(64), unique=True)
    idempotency_key: Mapped[str] = mapped_column(String(255))
    metadata_version: Mapped[int] = mapped_column(Integer, default=1)
    metadata_hash: Mapped[str] = mapped_column(String(64))
    input_hash: Mapped[str] = mapped_column(String(64))
    render_identity: Mapped[str] = mapped_column(String(64))
    caption_asset_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT")
    )
    caption_asset_sha256: Mapped[str | None] = mapped_column(String(64))
    thumbnail_asset_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT")
    )
    thumbnail_asset_sha256: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default=PublicationStatus.DRAFT.value)
    #: The state this run was in before ``status``. Present so the transition
    #: matrix can be a database constraint rather than only Python.
    previous_status: Mapped[str | None] = mapped_column(String(32))
    current_phase: Mapped[str] = mapped_column(
        String(32), default=PublicationPhase.ELIGIBILITY.value
    )
    video_id: Mapped[str | None] = mapped_column(String(128))
    processing_state: Mapped[str | None] = mapped_column(String(16))
    requested_privacy: Mapped[str] = mapped_column(String(16), default=PrivacyState.PRIVATE.value)
    #: What YouTube actually reports. Never inferred from the request.
    actual_privacy: Mapped[str | None] = mapped_column(String(16))
    scheduled_publish_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: True only once a user has explicitly asked for a non-private state.
    visibility_decision_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    visibility_decided_by: Mapped[str | None] = mapped_column(String(255))
    notify_subscribers: Mapped[bool] = mapped_column(Boolean, default=False)
    contains_synthetic_media: Mapped[bool] = mapped_column(Boolean, default=True)
    made_for_kids: Mapped[bool] = mapped_column(Boolean, default=False)
    #: The draft the user edits. Bounded, and never overwritten by a resume.
    draft_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    draft_edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    quota_units: Mapped[int] = mapped_column(BigInteger, default=0)
    capability_profile_version: Mapped[str] = mapped_column(String(64), default="")
    publisher_version: Mapped[str] = mapped_column(String(64), default="")
    gate_version: Mapped[str] = mapped_column(String(64), default="")
    error_code: Mapped[str | None] = mapped_column(String(128))
    error_summary: Mapped[str | None] = mapped_column(String(500))
    review_reason: Mapped[str | None] = mapped_column(String(500))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("project_id", "idempotency_key", name="uq_publication_run_idempotency"),
        # A YouTube video is published exactly once, by exactly one run.
        Index(
            "uq_publication_runs_video_id",
            "video_id",
            unique=True,
            postgresql_where=text("video_id IS NOT NULL"),
            sqlite_where=text("video_id IS NOT NULL"),
        ),
        Index("ix_publication_runs_project_status", "project_id", "status"),
        Index("ix_publication_runs_owner", "owner_subject", "status"),
        CheckConstraint(f"status IN {PUBLICATION_STATUSES}", name="publication_run_status"),
        CheckConstraint(
            f"previous_status IS NULL OR previous_status IN {PUBLICATION_STATUSES}",
            name="publication_run_previous_status",
        ),
        CheckConstraint(f"current_phase IN {PUBLICATION_PHASES}", name="publication_run_phase"),
        CheckConstraint(
            f"requested_privacy IN {PRIVACY_STATES}", name="publication_run_requested_privacy"
        ),
        CheckConstraint(
            f"actual_privacy IS NULL OR actual_privacy IN {PRIVACY_STATES}",
            name="publication_run_actual_privacy",
        ),
        CheckConstraint(
            f"processing_state IS NULL OR processing_state IN {PROCESSING_STATES}",
            name="publication_run_processing_state",
        ),
        CheckConstraint(
            "length(publication_identity) = 64 AND length(metadata_hash) = 64 "
            "AND length(input_hash) = 64 AND length(render_identity) = 64",
            name="publication_run_hash_lengths",
        ),
        CheckConstraint(
            "caption_asset_sha256 IS NULL OR length(caption_asset_sha256) = 64",
            name="publication_run_caption_hash_length",
        ),
        CheckConstraint(
            "thumbnail_asset_sha256 IS NULL OR length(thumbnail_asset_sha256) = 64",
            name="publication_run_thumbnail_hash_length",
        ),
        CheckConstraint("metadata_version >= 1", name="publication_run_metadata_version"),
        CheckConstraint("quota_units >= 0", name="publication_run_nonnegative_quota"),
        # The state machine, generated from the application's own table.
        CheckConstraint(transition_check_expression(), name="publication_run_transition"),
        # Every state after a completed upload names the video it created.
        CheckConstraint(
            f"status NOT IN {VIDEO_REQUIRED_STATUSES} OR video_id IS NOT NULL",
            name="publication_run_video_id_after_upload",
        ),
        # A published video is one YouTube finished processing.
        CheckConstraint(
            "status <> 'PUBLISHED' OR processing_state = 'succeeded'",
            name="publication_run_published_requires_processing",
        ),
        # A non-private state only exists after an explicit visibility decision.
        CheckConstraint(
            "actual_privacy IS NULL OR actual_privacy = 'private' "
            "OR visibility_decision_at IS NOT NULL",
            name="publication_run_public_requires_decision",
        ),
        CheckConstraint(
            "requested_privacy = 'private' OR visibility_decision_at IS NOT NULL "
            "OR status <> 'VISIBILITY_UPDATING'",
            name="publication_run_visibility_update_requires_decision",
        ),
        # A scheduled publication is only meaningful with a future-dated target.
        CheckConstraint(
            "scheduled_publish_at IS NULL OR requested_privacy = 'public'",
            name="publication_run_schedule_requires_public_target",
        ),
    )


class YouTubeUploadSession(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """The durable resumable-upload checkpoint for one publication run.

    The session URI is a bearer credential and is stored sealed. Only the
    publisher worker opens it; it is never projected, logged or traced.
    """

    __tablename__ = "youtube_upload_sessions"
    publication_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("publication_runs.id", ondelete="CASCADE"), index=True
    )
    session_uri_ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    session_uri_nonce: Mapped[bytes] = mapped_column(LargeBinary)
    #: SHA-256 of the plaintext URI: enough to prove two checkpoints refer to
    #: the same session, not enough to upload to it.
    session_uri_hash: Mapped[str] = mapped_column(String(64))
    encryption_key_version: Mapped[str] = mapped_column(String(64))
    total_bytes: Mapped[int] = mapped_column(BigInteger)
    #: The offset YouTube confirmed. Never the local optimistic count.
    confirmed_offset: Mapped[int] = mapped_column(BigInteger, default=0)
    chunk_bytes: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="active")
    last_response_code: Mapped[int | None] = mapped_column(Integer)
    video_id: Mapped[str | None] = mapped_column(String(128))
    provider_attempt_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("provider_attempts.id", ondelete="RESTRICT")
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        # One active session per run: a second initialization while one is live
        # is exactly the duplicate-video bug this table exists to prevent.
        Index(
            "uq_youtube_upload_sessions_active",
            "publication_run_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
            sqlite_where=text("status = 'active'"),
        ),
        UniqueConstraint(
            "publication_run_id", "session_uri_hash", name="uq_youtube_upload_session_identity"
        ),
        CheckConstraint(
            "status IN ('active','completed','expired','ambiguous','cancelled')",
            name="youtube_upload_session_status",
        ),
        CheckConstraint("total_bytes > 0", name="youtube_upload_session_positive_total"),
        CheckConstraint(
            "confirmed_offset >= 0 AND confirmed_offset <= total_bytes",
            name="youtube_upload_session_offset_within_total",
        ),
        CheckConstraint("chunk_bytes > 0", name="youtube_upload_session_positive_chunk"),
        CheckConstraint(
            "length(session_uri_ciphertext) > 0 AND length(session_uri_nonce) = 12",
            name="youtube_upload_session_sealed",
        ),
        CheckConstraint("length(session_uri_hash) = 64", name="youtube_upload_session_hash_length"),
        # A completed session confirmed every byte and names its video.
        CheckConstraint(
            "status <> 'completed' OR (confirmed_offset = total_bytes AND video_id IS NOT NULL)",
            name="youtube_upload_session_completed_is_whole",
        ),
    )


class PublicationAsset(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One uploaded YouTube sub-resource: the video, a caption or a thumbnail."""

    __tablename__ = "publication_assets"
    publication_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("publication_runs.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(16))
    local_asset_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT")
    )
    local_asset_sha256: Mapped[str | None] = mapped_column(String(64))
    provider_resource_id: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), default=PublicationAssetStatus.PENDING.value)
    provider_attempt_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("provider_attempts.id", ondelete="RESTRICT")
    )
    language: Mapped[str] = mapped_column(String(16), default="")
    name: Mapped[str] = mapped_column(String(150), default="")
    byte_size: Mapped[int] = mapped_column(BigInteger, default=0)
    projection: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(128))
    error_summary: Mapped[str | None] = mapped_column(String(500))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        # One row per kind per run for the video and the thumbnail: retrying a
        # successful operation reuses this row instead of uploading again.
        Index(
            "uq_publication_assets_singleton",
            "publication_run_id",
            "kind",
            unique=True,
            postgresql_where=text("kind IN ('video','thumbnail')"),
            sqlite_where=text("kind IN ('video','thumbnail')"),
        ),
        # One successful caption per publication, language and name. A retry
        # after a partial failure can never create a second identical track.
        Index(
            "uq_publication_assets_caption_identity",
            "publication_run_id",
            "kind",
            "language",
            "name",
            unique=True,
            postgresql_where=text("kind = 'caption' AND status = 'succeeded'"),
            sqlite_where=text("kind = 'caption' AND status = 'succeeded'"),
        ),
        Index("ix_publication_assets_run_kind", "publication_run_id", "kind"),
        CheckConstraint(f"kind IN {ASSET_KINDS}", name="publication_asset_kind"),
        CheckConstraint(f"status IN {ASSET_STATUSES}", name="publication_asset_status"),
        CheckConstraint("byte_size >= 0", name="publication_asset_nonnegative_size"),
        CheckConstraint(
            "local_asset_sha256 IS NULL OR length(local_asset_sha256) = 64",
            name="publication_asset_hash_length",
        ),
        # A succeeded video or caption always names the YouTube resource it
        # became; thumbnails.set returns no ID of its own.
        CheckConstraint(
            "status <> 'succeeded' OR kind = 'thumbnail' OR provider_resource_id IS NOT NULL",
            name="publication_asset_succeeded_has_resource",
        ),
        CheckConstraint(
            "status <> 'succeeded' OR completed_at IS NOT NULL",
            name="publication_asset_succeeded_has_completion",
        ),
        CheckConstraint(
            "status <> 'failed' OR error_code IS NOT NULL",
            name="publication_asset_failed_has_code",
        ),
        CheckConstraint(
            "kind <> 'caption' OR length(language) > 0", name="publication_asset_caption_language"
        ),
    )
