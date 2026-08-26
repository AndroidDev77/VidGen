"""T12 narration relational projections."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class VoiceProfileRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "voice_profiles"
    project_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(64))
    provider_voice_id: Mapped[str] = mapped_column(String(255))
    model: Mapped[str] = mapped_column(String(128))
    language: Mapped[str] = mapped_column(String(32))
    version: Mapped[int] = mapped_column(Integer)
    configuration: Mapped[dict[str, Any]] = mapped_column(JSON)
    configuration_hash: Mapped[str] = mapped_column(String(64))
    __table_args__ = (
        UniqueConstraint("id", "version", name="uq_voice_profile_version"),
        CheckConstraint("version > 0", name="positive_version"),
    )


class NarrationRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "narration_runs"
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    script_id: Mapped[UUID] = mapped_column(ForeignKey("scripts.id", ondelete="RESTRICT"))
    script_version: Mapped[int] = mapped_column(Integer)
    voice_profile_id: Mapped[UUID] = mapped_column(
        ForeignKey("voice_profiles.id", ondelete="RESTRICT")
    )
    voice_profile_version: Mapped[int] = mapped_column(Integer)
    idempotency_key: Mapped[str] = mapped_column(String(255))
    input_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(64))
    pipeline_version: Mapped[str] = mapped_column(String(32))
    selected: Mapped[bool] = mapped_column(Boolean, default=False)
    preview_asset_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT")
    )
    total_duration_seconds: Mapped[float | None] = mapped_column(Float)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(128))
    __table_args__ = (
        UniqueConstraint("project_id", "idempotency_key", name="uq_narration_run_idempotency"),
        Index(
            "uq_narration_selected_script",
            "project_id",
            "script_id",
            "script_version",
            unique=True,
            sqlite_where=text("selected = 1"),
            postgresql_where=text("selected"),
        ),
        CheckConstraint(
            "script_version > 0 AND voice_profile_version > 0", name="positive_versions"
        ),
        CheckConstraint(
            "total_duration_seconds IS NULL OR total_duration_seconds >= 0",
            name="nonnegative_duration",
        ),
    )


class NarrationSegment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "narration_segments"
    narration_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("narration_runs.id", ondelete="CASCADE")
    )
    script_segment_id: Mapped[UUID] = mapped_column(
        ForeignKey("script_segments.id", ondelete="RESTRICT")
    )
    sequence: Mapped[int] = mapped_column(Integer)
    text_hash: Mapped[str] = mapped_column(String(64))
    generation_identity: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(64))
    selected_attempt_id: Mapped[UUID | None] = mapped_column(nullable=True)
    original_asset_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT")
    )
    normalized_asset_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT")
    )
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    alignment: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    quality_report: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    word_timings: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    __table_args__ = (
        UniqueConstraint("narration_run_id", "sequence", name="uq_narration_segment_sequence"),
        UniqueConstraint(
            "narration_run_id", "script_segment_id", name="uq_narration_segment_script"
        ),
        CheckConstraint("sequence >= 0", name="nonnegative_sequence"),
        CheckConstraint(
            "duration_seconds IS NULL OR duration_seconds > 0", name="positive_duration"
        ),
    )


class NarrationAttemptRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "narration_attempts"
    narration_segment_id: Mapped[UUID] = mapped_column(
        ForeignKey("narration_segments.id", ondelete="CASCADE")
    )
    attempt_number: Mapped[int] = mapped_column(Integer)
    provider: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(128))
    provider_request_id: Mapped[str | None] = mapped_column(String(255))
    provider_idempotency_key: Mapped[str] = mapped_column(String(255), unique=True)
    voice_settings: Mapped[dict[str, Any]] = mapped_column(JSON)
    instructions: Mapped[str] = mapped_column(Text)
    provider_output_asset_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT")
    )
    normalized_asset_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT")
    )
    usage: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    quality_result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    failure_classification: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(128))
    completed_at: Mapped[str | None] = mapped_column(String(64))
    __table_args__ = (
        UniqueConstraint(
            "narration_segment_id", "attempt_number", name="uq_narration_attempt_number"
        ),
        CheckConstraint("attempt_number BETWEEN 1 AND 3", name="attempt_range"),
    )
