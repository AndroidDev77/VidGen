from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from vidgen.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

asset_dependencies = Table(
    "asset_dependencies",
    Base.metadata,
    Column("asset_id", ForeignKey("assets.id", ondelete="CASCADE"), primary_key=True),
    Column("parent_asset_id", ForeignKey("assets.id", ondelete="RESTRICT"), primary_key=True),
    CheckConstraint("asset_id <> parent_asset_id", name="not_self_parent"),
)


class Project(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "projects"
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    owner_subject: Mapped[str] = mapped_column(String(255), default="local-user", nullable=False)
    status: Mapped[str] = mapped_column(String(64), default="uploaded", nullable=False, index=True)
    target_duration_seconds: Mapped[float] = mapped_column(Float, default=300, nullable=False)
    visual_style: Mapped[str] = mapped_column(Text, nullable=False)
    humor_intensity: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    settings: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    __table_args__ = (
        CheckConstraint("target_duration_seconds > 0", name="positive_target_duration"),
        CheckConstraint("humor_intensity BETWEEN 0 AND 10", name="humor_range"),
    )


class Asset(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "assets"
    project_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True
    )
    kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    media_type: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str | None] = mapped_column(String(128))
    provider_request_id: Mapped[str | None] = mapped_column(String(255))
    idempotency_key: Mapped[str | None] = mapped_column(String(255), index=True)
    generation_parameters: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, nullable=False
    )
    extra_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    parents: Mapped[list[Asset]] = relationship(
        "Asset",
        secondary=asset_dependencies,
        primaryjoin=lambda: Asset.id == asset_dependencies.c.asset_id,
        secondaryjoin=lambda: Asset.id == asset_dependencies.c.parent_asset_id,
        lazy="selectin",
    )
    __table_args__ = (
        CheckConstraint("byte_size >= 0", name="nonnegative_byte_size"),
        CheckConstraint("length(sha256) = 64", name="sha256_length"),
        Index(
            "uq_assets_project_idempotency",
            "project_id",
            "idempotency_key",
            unique=True,
            postgresql_where=sql_text("idempotency_key IS NOT NULL"),
            sqlite_where=sql_text("idempotency_key IS NOT NULL"),
        ),
    )


class SourceVideo(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "source_videos"
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT"), unique=True
    )
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    frame_rate: Mapped[float | None] = mapped_column(Float)
    probe: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class Character(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "characters"
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    canonical_name: Mapped[str] = mapped_column(String(255), nullable=False)
    definition: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    bible_asset_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="SET NULL")
    )
    __table_args__ = (UniqueConstraint("project_id", "canonical_name"),)


class Location(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "locations"
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    canonical_name: Mapped[str] = mapped_column(String(255), nullable=False)
    definition: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    reference_asset_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="SET NULL")
    )
    __table_args__ = (UniqueConstraint("project_id", "canonical_name"),)


class Scene(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "scenes"
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    source_start_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    source_end_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    location_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("locations.id", ondelete="SET NULL")
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    analysis: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    __table_args__ = (
        UniqueConstraint("project_id", "sequence"),
        CheckConstraint("source_end_seconds > source_start_seconds", name="valid_time_range"),
    )


class PlotBeat(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "plot_beats"
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    importance: Mapped[float] = mapped_column(Float, nullable=False)
    required_for_coherence: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    scene_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    __table_args__ = (
        UniqueConstraint("project_id", "sequence"),
        CheckConstraint("importance BETWEEN 0 AND 1", name="importance_range"),
    )


class Script(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "scripts"
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(64), default="draft", nullable=False)
    contract: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    __table_args__ = (UniqueConstraint("project_id", "revision"),)


class ScriptSegment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "script_segments"
    script_id: Mapped[UUID] = mapped_column(
        ForeignKey("scripts.id", ondelete="CASCADE"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    narration: Mapped[str] = mapped_column(Text, nullable=False)
    target_duration_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    measured_duration_seconds: Mapped[float | None] = mapped_column(Float)
    contract: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    __table_args__ = (
        UniqueConstraint("script_id", "sequence"),
        CheckConstraint("target_duration_seconds > 0", name="positive_target_duration"),
    )


class Shot(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "shots"
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    script_segment_id: Mapped[UUID] = mapped_column(
        ForeignKey("script_segments.id", ondelete="CASCADE"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(64), default="pending", nullable=False, index=True)
    duration_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    contract: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    selected_image_id: Mapped[UUID | None] = mapped_column(
        ForeignKey(
            "generated_images.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_shots_selected_image_id_generated_images",
        )
    )
    selected_video_id: Mapped[UUID | None] = mapped_column(
        ForeignKey(
            "generated_videos.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_shots_selected_video_id_generated_videos",
        )
    )
    __table_args__ = (
        UniqueConstraint("project_id", "sequence"),
        CheckConstraint("duration_seconds > 0", name="positive_duration"),
        Index(
            "ix_shots_selected_image",
            "selected_image_id",
            postgresql_where=sql_text("selected_image_id IS NOT NULL"),
            sqlite_where=sql_text("selected_image_id IS NOT NULL"),
        ),
        Index(
            "ix_shots_selected_video",
            "selected_video_id",
            postgresql_where=sql_text("selected_video_id IS NOT NULL"),
            sqlite_where=sql_text("selected_video_id IS NOT NULL"),
        ),
    )


class GeneratedImage(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "generated_images"
    shot_id: Mapped[UUID] = mapped_column(ForeignKey("shots.id", ondelete="CASCADE"), index=True)
    asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT"), unique=True
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    seed: Mapped[int | None] = mapped_column(Integer)
    provider: Mapped[str] = mapped_column(String(128), nullable=False)
    provider_request_id: Mapped[str | None] = mapped_column(String(255))
    __table_args__ = (UniqueConstraint("shot_id", "attempt"),)


class GeneratedVideo(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "generated_videos"
    shot_id: Mapped[UUID] = mapped_column(ForeignKey("shots.id", ondelete="CASCADE"), index=True)
    source_image_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("generated_images.id", ondelete="SET NULL")
    )
    asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT"), unique=True
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    duration_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    provider: Mapped[str] = mapped_column(String(128), nullable=False)
    provider_request_id: Mapped[str | None] = mapped_column(String(255))
    __table_args__ = (UniqueConstraint("shot_id", "attempt"),)


class AudioAsset(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "audio_assets"
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    script_segment_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("script_segments.id", ondelete="CASCADE"), index=True
    )
    asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT"), unique=True
    )
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    duration_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    provider: Mapped[str | None] = mapped_column(String(128))
    provider_request_id: Mapped[str | None] = mapped_column(String(255))


class QAResult(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "qa_results"
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    shot_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("shots.id", ondelete="CASCADE"), index=True
    )
    asset_id: Mapped[UUID] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), index=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    decision: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    overall_score: Mapped[float] = mapped_column(Float, nullable=False)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    __table_args__ = (CheckConstraint("overall_score BETWEEN 0 AND 1", name="score_range"),)


class RenderJob(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "render_jobs"
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(64), default="pending", nullable=False, index=True)
    attempt: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    manifest_asset_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="SET NULL")
    )
    output_asset_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="SET NULL")
    )
    error: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
