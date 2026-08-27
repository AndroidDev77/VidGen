"""Restartable relational projections for T14; T23 owns cost records."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from vidgen.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ImageGenerationRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "image_generation_runs"
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    storyboard_id: Mapped[UUID] = mapped_column(
        ForeignKey("storyboard_runs.id", ondelete="RESTRICT")
    )
    storyboard_version: Mapped[int] = mapped_column(Integer)
    idempotency_key: Mapped[str] = mapped_column(String(255))
    input_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))
    provider_configuration_version: Mapped[str] = mapped_column(String(64))
    prompt_compiler_version: Mapped[str] = mapped_column(String(32))
    pipeline_version: Mapped[str] = mapped_column(String(32))
    requested_item_count: Mapped[int] = mapped_column(Integer, default=0)
    completed_item_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_item_count: Mapped[int] = mapped_column(Integer, default=0)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(128))
    __table_args__ = (
        UniqueConstraint("project_id", "idempotency_key", name="uq_image_run_idempotency"),
        CheckConstraint("storyboard_version > 0", name="image_run_positive_version"),
        CheckConstraint("length(input_hash) = 64", name="image_run_hash_length"),
    )


class ImageGenerationItem(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "image_generation_items"
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("image_generation_runs.id", ondelete="CASCADE"), index=True
    )
    shot_id: Mapped[UUID] = mapped_column(ForeignKey("storyboard_shots.id", ondelete="RESTRICT"))
    shot_sequence: Mapped[int] = mapped_column(Integer)
    keyframe_role: Mapped[str] = mapped_column(String(16))
    generation_identity: Mapped[str] = mapped_column(String(64), unique=True)
    input_hash: Mapped[str] = mapped_column(String(64))
    prompt_package: Mapped[dict[str, Any]] = mapped_column(JSON)
    provider_result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    selected_generated_image_id: Mapped[UUID | None] = mapped_column(nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(128))
    __table_args__ = (
        UniqueConstraint("run_id", "shot_id", "keyframe_role", name="uq_image_item_shot_role"),
        CheckConstraint("keyframe_role IN ('FIRST_FRAME','LAST_FRAME')", name="image_item_role"),
        CheckConstraint("shot_sequence >= 0 AND attempt_count >= 0", name="image_item_counts"),
    )


class GeneratedKeyframeImage(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "generated_keyframe_images"
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    shot_id: Mapped[UUID] = mapped_column(ForeignKey("storyboard_shots.id", ondelete="RESTRICT"))
    keyframe_role: Mapped[str] = mapped_column(String(16))
    item_id: Mapped[UUID] = mapped_column(
        ForeignKey("image_generation_items.id", ondelete="CASCADE"), unique=True
    )
    provider_attempt_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("provider_attempts.id", ondelete="SET NULL")
    )
    asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT"), unique=True
    )
    provider: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(128))
    prompt_hash: Mapped[str] = mapped_column(String(64))
    reference_hash: Mapped[str] = mapped_column(String(64))
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    mime_type: Mapped[str] = mapped_column(String(64))
    byte_size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    validation_report: Mapped[dict[str, Any]] = mapped_column(JSON)
    selected: Mapped[bool] = mapped_column(Boolean, default=True)
    __table_args__ = (
        CheckConstraint(
            "width > 0 AND height > 0 AND byte_size > 0", name="generated_image_positive_geometry"
        ),
        Index(
            "uq_generated_keyframe_selected",
            "shot_id",
            "keyframe_role",
            unique=True,
            sqlite_where=text("selected = 1"),
            postgresql_where=text("selected"),
        ),
    )
