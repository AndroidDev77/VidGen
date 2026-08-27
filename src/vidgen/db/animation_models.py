"""Restartable relational projections for T15 animation."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from vidgen.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class AnimationRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "animation_runs"
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    storyboard_id: Mapped[UUID] = mapped_column(
        ForeignKey("storyboard_runs.id", ondelete="RESTRICT")
    )
    storyboard_version: Mapped[int] = mapped_column(Integer)
    image_generation_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("image_generation_runs.id", ondelete="RESTRICT")
    )
    idempotency_key: Mapped[str] = mapped_column(String(255))
    input_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(64))
    routing_policy_version: Mapped[str] = mapped_column(String(64))
    provider_configuration_version: Mapped[str] = mapped_column(String(64))
    pipeline_version: Mapped[str] = mapped_column(String(32))
    requested_item_count: Mapped[int] = mapped_column(Integer, default=0)
    completed_item_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_item_count: Mapped[int] = mapped_column(Integer, default=0)
    original_video_count: Mapped[int] = mapped_column(Integer, default=0)
    canonical_video_count: Mapped[int] = mapped_column(Integer, default=0)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(128))
    __table_args__ = (
        UniqueConstraint("project_id", "idempotency_key", name="uq_animation_run_idempotency"),
        CheckConstraint("storyboard_version > 0", name="animation_run_positive_version"),
        CheckConstraint("length(input_hash) = 64", name="animation_run_hash_length"),
        CheckConstraint(
            "requested_item_count >= 0 AND completed_item_count >= 0 "
            "AND failed_item_count >= 0 AND original_video_count >= 0 "
            "AND canonical_video_count >= 0",
            name="animation_run_nonnegative_counts",
        ),
    )


class AnimationItem(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "animation_items"
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("animation_runs.id", ondelete="CASCADE"), index=True
    )
    shot_id: Mapped[UUID] = mapped_column(ForeignKey("storyboard_shots.id", ondelete="RESTRICT"))
    shot_sequence: Mapped[int] = mapped_column(Integer)
    first_keyframe_asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT")
    )
    last_keyframe_asset_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT")
    )
    generation_identity: Mapped[str] = mapped_column(String(64), unique=True)
    motion_prompt_hash: Mapped[str] = mapped_column(String(64))
    motion_prompt_package: Mapped[dict[str, Any]] = mapped_column(JSON)
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(32))
    requested_duration: Mapped[float] = mapped_column(Float)
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(64))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    selected_generated_video_id: Mapped[UUID | None] = mapped_column(
        ForeignKey(
            "animation_generated_videos.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_animation_items_selected_generated_video",
        ),
        nullable=True,
    )
    error_code: Mapped[str | None] = mapped_column(String(128))
    warnings: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    __table_args__ = (
        UniqueConstraint("run_id", "shot_id", name="uq_animation_item_shot"),
        CheckConstraint("shot_sequence >= 0", name="animation_item_sequence"),
        CheckConstraint("attempt_count >= 0", name="animation_item_attempt_count"),
        CheckConstraint(
            "requested_duration > 0 AND width > 0 AND height > 0",
            name="animation_item_positive_media",
        ),
    )


class RunwayTask(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "runway_tasks"
    animation_item_id: Mapped[UUID] = mapped_column(
        ForeignKey("animation_items.id", ondelete="CASCADE"), index=True
    )
    provider_attempt_id: Mapped[UUID] = mapped_column(
        ForeignKey("provider_attempts.id", ondelete="RESTRICT"), unique=True
    )
    remote_task_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    provider_status: Mapped[str] = mapped_column(String(32))
    request_projection: Mapped[dict[str, Any]] = mapped_column(JSON)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    poll_count: Mapped[int] = mapped_column(Integer, default=0)
    progress: Mapped[float | None] = mapped_column(Float)
    terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_code: Mapped[str | None] = mapped_column(String(128))
    failure_message: Mapped[str | None] = mapped_column(String(1024))
    response_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    cancellation_status: Mapped[str | None] = mapped_column(String(32))
    __table_args__ = (
        UniqueConstraint(
            "animation_item_id", "provider_attempt_id", name="uq_runway_task_item_attempt"
        ),
        CheckConstraint("poll_count >= 0", name="runway_task_poll_count"),
        CheckConstraint(
            "progress IS NULL OR (progress >= 0 AND progress <= 1)",
            name="runway_task_progress",
        ),
    )


class AnimationGeneratedVideo(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """T15 detail projection extending the legacy generated_videos placeholder."""

    __tablename__ = "animation_generated_videos"
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    shot_id: Mapped[UUID] = mapped_column(ForeignKey("storyboard_shots.id", ondelete="RESTRICT"))
    animation_item_id: Mapped[UUID] = mapped_column(
        ForeignKey("animation_items.id", ondelete="CASCADE"), unique=True
    )
    provider_attempt_id: Mapped[UUID] = mapped_column(
        ForeignKey("provider_attempts.id", ondelete="RESTRICT")
    )
    remote_task_id: Mapped[str] = mapped_column(String(255), unique=True)
    original_asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT"), unique=True
    )
    canonical_asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT"), unique=True
    )
    requested_duration: Mapped[float] = mapped_column(Float)
    provider_duration: Mapped[float] = mapped_column(Float)
    canonical_duration: Mapped[float] = mapped_column(Float)
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    codec: Mapped[str] = mapped_column(String(32))
    container: Mapped[str] = mapped_column(String(32))
    frame_rate: Mapped[str] = mapped_column(String(32))
    sha256: Mapped[str] = mapped_column(String(64))
    validation_report: Mapped[dict[str, Any]] = mapped_column(JSON)
    trim_manifest: Mapped[dict[str, Any]] = mapped_column(JSON)
    selected: Mapped[bool] = mapped_column(Boolean, default=True)
    __table_args__ = (
        CheckConstraint(
            "requested_duration > 0 AND provider_duration > 0 "
            "AND canonical_duration > 0 AND width > 0 AND height > 0",
            name="animation_generated_video_positive_media",
        ),
        CheckConstraint("length(sha256) = 64", name="animation_generated_video_hash_length"),
        Index(
            "uq_animation_generated_video_selected",
            "shot_id",
            unique=True,
            sqlite_where=text("selected = 1"),
            postgresql_where=text("selected"),
        ),
    )
