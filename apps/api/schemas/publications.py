"""Bounded T25 publication API projections.

Deliberately narrow: no access token, no refresh token, no authorization code,
no resumable session URI, no client secret and no raw YouTube payload appears in
any request or response model here. The dashboard gets IDs, states, counters and
the public watch URL; anything else it needs, it asks a purpose-built endpoint
for.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field

from vidgen.contracts.common import StrictContract

PrivacyLiteral = Literal["private", "unlisted", "public"]


# -- connections ---------------------------------------------------------------
class YouTubeChannelProjection(StrictContract):
    channel_id: str = Field(max_length=128)
    title: str = Field(default="", max_length=255)
    thumbnail_url: str = Field(default="", max_length=1000)
    custom_url: str = Field(default="", max_length=255)


class YouTubeConnectionProjection(StrictContract):
    connection_id: UUID
    channel: YouTubeChannelProjection
    status: str = Field(max_length=32)
    granted_scopes: list[str] = Field(default_factory=list, max_length=16)
    #: Only the *version* of the envelope key, never the key or the ciphertext.
    encryption_key_version: str = Field(default="", max_length=64)
    credential_expires_at: datetime | None = None
    last_verified_at: datetime | None = None
    error_code: str | None = Field(default=None, max_length=128)
    created_at: datetime
    updated_at: datetime


class YouTubeConnectionCollection(StrictContract):
    items: list[YouTubeConnectionProjection] = Field(default_factory=list, max_length=32)
    #: Whether this deployment can start an authorization at all. False when the
    #: OAuth client is unconfigured, so the UI explains rather than failing.
    oauth_configured: bool = False
    #: Surfaced so the dashboard can state the current limitation honestly.
    production_authentication_available: bool = False


class OAuthStartRequest(StrictContract):
    """Start an authorization. The redirect target is allowlist-checked."""

    redirect_target: str = Field(default="", max_length=1000)


class OAuthStartResponse(StrictContract):
    state_id: UUID
    #: Google's authorization URL. Carries the public client ID, the scopes, the
    #: state and the PKCE challenge; never a secret and never the verifier.
    authorization_url: str = Field(max_length=2000)
    expires_at: datetime
    row_version: int = Field(ge=0)


class OAuthCallbackResponse(StrictContract):
    connection_id: UUID
    channel: YouTubeChannelProjection
    status: str = Field(max_length=32)
    redirect_target: str = Field(default="", max_length=1000)


class DisconnectResponse(StrictContract):
    connection_id: UUID
    status: str = Field(max_length=32)
    revoked: bool = False


# -- publications --------------------------------------------------------------
class PublicationMetadataRequest(StrictContract):
    """The editable draft. Limits are re-checked against the capability registry."""

    title: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=5000)
    tags: list[str] = Field(default_factory=list, max_length=60)
    category_id: str = Field(default="24", min_length=1, max_length=8, pattern=r"^[0-9]+$")
    default_language: str = Field(default="en", min_length=2, max_length=16)
    caption_language: str = Field(default="en", min_length=2, max_length=16)
    caption_track_name: str = Field(default="VidGen recap", min_length=1, max_length=150)
    made_for_kids: bool = False
    contains_synthetic_media: bool = True
    embeddable: bool = True
    notify_subscribers: bool = False
    requested_privacy: PrivacyLiteral = "private"
    scheduled_publish_at: datetime | None = None


class PublicationCreateRequest(StrictContract):
    connection_id: UUID
    thumbnail_asset_id: UUID | None = None
    #: Omit to accept the deterministic draft built from project metadata.
    metadata: PublicationMetadataRequest | None = None


class PublicationStartRequest(StrictContract):
    """Start or continue the upload. The provider is deployment configuration."""

    resume: bool = False


class PublicationCancelRequest(StrictContract):
    reason: str = Field(default="", max_length=500)


class PublicationVisibilityRequest(StrictContract):
    """An explicit visibility decision. Public and unlisted are never implicit."""

    privacy: PrivacyLiteral
    scheduled_publish_at: datetime | None = None
    notify_subscribers: bool = False


class PublicationFailureProjection(StrictContract):
    code: str = Field(max_length=64)
    summary: str = Field(default="", max_length=500)
    retryable: bool = False
    http_status: int | None = Field(default=None, ge=100, le=599)
    remediation: str = Field(default="", max_length=500)


class PublicationAssetProjection(StrictContract):
    kind: str = Field(max_length=16)
    status: str = Field(max_length=16)
    local_asset_id: UUID | None = None
    provider_resource_id: str = Field(default="", max_length=128)
    language: str = Field(default="", max_length=16)
    name: str = Field(default="", max_length=150)
    byte_size: int = Field(default=0, ge=0)
    error_code: str | None = Field(default=None, max_length=128)
    error_summary: str | None = Field(default=None, max_length=500)


class PublicationAttemptProjection(StrictContract):
    attempt_id: UUID
    operation: str = Field(max_length=64)
    status: str = Field(max_length=16)
    provider: str = Field(max_length=64)
    latency_ms: int = Field(default=0, ge=0)
    #: A rate-limit quantity, never a monetary charge.
    quota_units: int = Field(default=0, ge=0)
    failure_code: str | None = Field(default=None, max_length=64)
    started_at: datetime
    completed_at: datetime | None = None


class PublicationProjection(StrictContract):
    publication_id: UUID
    project_id: UUID
    connection_id: UUID
    channel_id: str = Field(max_length=128)
    final_render_asset_id: UUID
    final_editorial_run_id: UUID
    approval_id: UUID | None = None
    publication_identity: str = Field(max_length=64)
    metadata_version: int = Field(ge=1)
    status: str = Field(max_length=32)
    phase: str = Field(max_length=32)
    video_id: str | None = Field(default=None, max_length=128)
    video_url: str = Field(default="", max_length=255)
    total_bytes: int = Field(default=0, ge=0)
    confirmed_offset: int = Field(default=0, ge=0)
    processing_state: str | None = Field(default=None, max_length=16)
    caption_status: str | None = Field(default=None, max_length=16)
    caption_track_id: str = Field(default="", max_length=128)
    thumbnail_status: str | None = Field(default=None, max_length=16)
    requested_privacy: PrivacyLiteral = "private"
    actual_privacy: PrivacyLiteral | None = None
    scheduled_publish_at: datetime | None = None
    contains_synthetic_media: bool = True
    made_for_kids: bool = False
    notify_subscribers: bool = False
    quota_units: int = Field(default=0, ge=0)
    capability_profile_version: str = Field(default="", max_length=64)
    publisher_version: str = Field(default="", max_length=64)
    gate_version: str = Field(default="", max_length=64)
    render_identity: str = Field(default="", max_length=64)
    metadata: PublicationMetadataRequest | None = None
    failure: PublicationFailureProjection | None = None
    row_version: int = Field(default=0, ge=0)
    created_at: datetime
    updated_at: datetime


class PublicationDetailProjection(PublicationProjection):
    assets: list[PublicationAssetProjection] = Field(default_factory=list, max_length=16)
    attempts: list[PublicationAttemptProjection] = Field(default_factory=list, max_length=64)


class PublicationCollectionResponse(StrictContract):
    project_id: UUID
    items: list[PublicationProjection] = Field(default_factory=list, max_length=64)
    #: The live gate, so the dashboard can explain a disabled publish button.
    gate: PublicationGateProjection


class PublicationGateProjection(StrictContract):
    project_id: UUID
    allowed: bool
    final_render_asset_id: UUID | None = None
    final_editorial_run_id: UUID | None = None
    approval_id: UUID | None = None
    caption_asset_id: UUID | None = None
    gate_version: str = Field(default="", max_length=64)
    failures: list[PublicationFailureProjection] = Field(default_factory=list, max_length=32)
    warnings: list[str] = Field(default_factory=list, max_length=32)
    row_version: int = Field(default=0, ge=0)


PublicationCollectionResponse.model_rebuild()
