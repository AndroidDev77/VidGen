"""T17 attempt and caption-track relational projections."""

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


class RenderAttempt(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "render_attempts"
    render_job_id: Mapped[UUID] = mapped_column(
        ForeignKey("render_jobs.id", ondelete="CASCADE"), index=True
    )
    attempt_number: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(64))
    manifest_hash: Mapped[str] = mapped_column(String(64))
    command_plan_hash: Mapped[str | None] = mapped_column(String(64))
    operational_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    ffmpeg_version: Mapped[str | None] = mapped_column(String(255))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_classification: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(128))
    diagnostic_asset_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT")
    )
    __table_args__ = (
        UniqueConstraint("render_job_id", "attempt_number", name="uq_render_attempt_number"),
        CheckConstraint("attempt_number > 0", name="render_attempt_positive"),
        CheckConstraint("length(manifest_hash) = 64", name="render_attempt_manifest_hash"),
        CheckConstraint(
            "command_plan_hash IS NULL OR length(command_plan_hash) = 64",
            name="render_attempt_plan_hash",
        ),
    )


class CaptionTrackRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "caption_tracks"
    render_job_id: Mapped[UUID] = mapped_column(
        ForeignKey("render_jobs.id", ondelete="CASCADE"), unique=True
    )
    narration_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("narration_runs.id", ondelete="RESTRICT"), index=True
    )
    caption_identity: Mapped[str] = mapped_column(String(64), unique=True)
    language: Mapped[str] = mapped_column(String(16))
    cue_count: Mapped[int] = mapped_column(Integer)
    start_us: Mapped[int] = mapped_column(Integer)
    end_us: Mapped[int] = mapped_column(Integer)
    srt_asset_id: Mapped[UUID] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    webvtt_asset_id: Mapped[UUID] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    ass_asset_id: Mapped[UUID | None] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    validation_report_asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT")
    )
    configuration_hash: Mapped[str] = mapped_column(String(64))
    __table_args__ = (
        CheckConstraint(
            "cue_count > 0 AND start_us >= 0 AND end_us > start_us",
            name="caption_track_valid_range",
        ),
        CheckConstraint(
            "length(caption_identity) = 64 AND length(configuration_hash) = 64",
            name="caption_track_hashes",
        ),
        CheckConstraint("length(language) BETWEEN 2 AND 16", name="caption_track_language"),
        Index("ix_caption_tracks_render_job", "render_job_id"),
    )
