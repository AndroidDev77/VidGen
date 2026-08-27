"""T18 review-UI control-plane persistence.

These tables add only what the existing schema cannot represent: monotonic row
versions for the resources the review UI mutates, replayable API idempotency
records, a bounded durable project event log for Server-Sent Events, render
approvals, and the downstream invalidation records a targeted regeneration or a
script edit produces. Workflow runs, assets, costs, and provider attempts are
reused from T05-T17 and T23 rather than duplicated.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from vidgen.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

# Bounded projections only: event payloads never carry transcript text, script
# text, prompts, signed URLs, or provider responses.
MAX_EVENT_PAYLOAD_BYTES = 4096

RESOURCE_TYPES = (
    "project",
    "transcript",
    "transcript_segment",
    "script",
    "script_segment",
    "storyboard",
    "shot",
    "render",
)


class ResourceVersion(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Monotonic optimistic-concurrency version for one owner-scoped resource."""

    __tablename__ = "resource_versions"
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[UUID] = mapped_column(nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    __table_args__ = (
        UniqueConstraint("resource_type", "resource_id", name="uq_resource_versions_identity"),
        CheckConstraint("version > 0", name="resource_version_positive"),
        CheckConstraint(
            "resource_type IN ("
            "'project','transcript','transcript_segment','script','script_segment',"
            "'storyboard','shot','render')",
            name="resource_version_known_type",
        ),
    )


class ApiIdempotencyRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Replay record binding one client key to one owner, operation and request."""

    __tablename__ = "api_idempotency_records"
    owner_subject: Mapped[str] = mapped_column(String(255), nullable=False)
    operation: Mapped[str] = mapped_column(String(128), nullable=False)
    resource_key: Mapped[str] = mapped_column(String(255), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    response_body: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    __table_args__ = (
        UniqueConstraint(
            "owner_subject",
            "operation",
            "resource_key",
            "idempotency_key",
            name="uq_api_idempotency_scope",
        ),
        CheckConstraint("length(request_hash) = 64", name="api_idempotency_hash_length"),
        CheckConstraint(
            "status_code >= 100 AND status_code < 600", name="api_idempotency_status_range"
        ),
    )


class ProjectUIEvent(UUIDPrimaryKeyMixin, Base):
    """Durable, ordered, bounded projection streamed to the review UI."""

    __tablename__ = "project_ui_events"
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    stage: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    workflow_id: Mapped[str | None] = mapped_column(String(255))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        UniqueConstraint("project_id", "sequence", name="uq_project_ui_events_sequence"),
        CheckConstraint("sequence > 0", name="project_ui_event_positive_sequence"),
        Index("ix_project_ui_events_project_sequence", "project_id", "sequence"),
    )


class RenderApproval(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Historical approval of one verified T17 render by one principal."""

    __tablename__ = "render_approvals"
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    render_job_id: Mapped[UUID] = mapped_column(
        ForeignKey("render_jobs.id", ondelete="RESTRICT"), nullable=False
    )
    approved_by: Mapped[str] = mapped_column(String(255), nullable=False)
    lineage_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint(
            "render_job_id", "lineage_hash", name="uq_render_approvals_render_lineage"
        ),
        CheckConstraint("length(lineage_hash) = 64", name="render_approval_lineage_hash_length"),
    )


class DownstreamInvalidation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One recorded stale-marking caused by an edit or a targeted regeneration."""

    __tablename__ = "downstream_invalidations"
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    origin_type: Mapped[str] = mapped_column(String(64), nullable=False)
    origin_id: Mapped[UUID] = mapped_column(nullable=False)
    invalidated_type: Mapped[str] = mapped_column(String(64), nullable=False)
    invalidated_id: Mapped[UUID] = mapped_column(nullable=False)
    reason: Mapped[str] = mapped_column(String(128), nullable=False)
    __table_args__ = (
        Index(
            "ix_downstream_invalidations_origin",
            "project_id",
            "origin_type",
            "origin_id",
        ),
    )
