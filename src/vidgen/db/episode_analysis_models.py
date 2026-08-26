from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import JSON, Boolean, CheckConstraint, ForeignKey, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from vidgen.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class EpisodeAnalysisRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "episode_analysis_runs"
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    source_video_id: Mapped[UUID] = mapped_column(
        ForeignKey("source_videos.id", ondelete="RESTRICT")
    )
    evidence_package_id: Mapped[UUID] = mapped_column(
        ForeignKey("evidence_packages.id", ondelete="RESTRICT")
    )
    idempotency_key: Mapped[str] = mapped_column(String(255))
    input_hash: Mapped[str] = mapped_column(String(64))
    contract_version: Mapped[str] = mapped_column(String(32))
    prompt_version: Mapped[str] = mapped_column(String(32))
    provider_configuration_version: Mapped[str] = mapped_column(String(64))
    provider: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(64))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    validation_report: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String(64))
    selected: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (
        Index("uq_analysis_run_project_idempotency", "project_id", "idempotency_key", unique=True),
        CheckConstraint("attempt_count >= 0", name="analysis_attempt_nonnegative"),
    )


class SceneAnalysisCheckpoint(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "scene_analysis_checkpoints"
    analysis_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("episode_analysis_runs.id", ondelete="CASCADE")
    )
    source_scene_id: Mapped[UUID]
    sequence: Mapped[int] = mapped_column(Integer)
    input_hash: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True)
    status: Mapped[str] = mapped_column(String(32))
    provider_request_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    provider_result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    validation_report: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String(64))
    __table_args__ = (
        Index("uq_checkpoint_run_scene", "analysis_run_id", "source_scene_id", unique=True),
        CheckConstraint("sequence > 0", name="checkpoint_positive_sequence"),
        CheckConstraint("attempt_count >= 0", name="checkpoint_attempt_nonnegative"),
    )


class EpisodeAnalysisRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "episode_analyses"
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    analysis_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("episode_analysis_runs.id", ondelete="RESTRICT"), unique=True
    )
    version: Mapped[int] = mapped_column(Integer)
    canonical_analysis_asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT")
    )
    input_hash: Mapped[str] = mapped_column(String(64))
    duration_ms: Mapped[int] = mapped_column(Integer)
    character_count: Mapped[int] = mapped_column(Integer)
    location_count: Mapped[int] = mapped_column(Integer)
    scene_count: Mapped[int] = mapped_column(Integer)
    plot_beat_count: Mapped[int] = mapped_column(Integer)
    selected: Mapped[bool] = mapped_column(Boolean, default=False)
    warnings: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    __table_args__ = (
        Index("uq_episode_analysis_project_version", "project_id", "version", unique=True),
        Index(
            "uq_episode_analysis_selected",
            "project_id",
            unique=True,
            sqlite_where=text("selected = 1"),
            postgresql_where=text("selected"),
        ),
        CheckConstraint("version > 0", name="analysis_positive_version"),
        CheckConstraint("duration_ms > 0", name="analysis_positive_duration"),
    )
