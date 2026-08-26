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
    text,
)
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
        CheckConstraint("length(input_hash) = 64", name="analysis_run_input_hash_length"),
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
        CheckConstraint("length(input_hash) = 64", name="checkpoint_input_hash_length"),
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
        CheckConstraint("length(input_hash) = 64", name="analysis_input_hash_length"),
        CheckConstraint(
            "character_count >= 0 AND location_count >= 0 "
            "AND scene_count > 0 AND plot_beat_count >= 0",
            name="analysis_valid_counts",
        ),
    )


class AnalysisStateEvent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "analysis_state_events"
    analysis_id: Mapped[UUID] = mapped_column(ForeignKey("episode_analyses.id", ondelete="CASCADE"))
    stable_id: Mapped[UUID]
    entity_id: Mapped[UUID]
    scene_id: Mapped[UUID] = mapped_column(ForeignKey("scene_evidence.id", ondelete="RESTRICT"))
    sequence: Mapped[int] = mapped_column(Integer)
    confidence: Mapped[float] = mapped_column(Float)
    contract: Mapped[dict[str, Any]] = mapped_column(JSON)
    __table_args__ = (
        Index("uq_analysis_state_stable", "analysis_id", "stable_id", unique=True),
        CheckConstraint("sequence > 0", name="analysis_state_positive_sequence"),
        CheckConstraint("confidence BETWEEN 0 AND 1", name="analysis_state_confidence"),
    )


class AnalysisRelationship(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "analysis_relationships"
    analysis_id: Mapped[UUID] = mapped_column(ForeignKey("episode_analyses.id", ondelete="CASCADE"))
    stable_id: Mapped[UUID]
    source_character_id: Mapped[UUID] = mapped_column(
        ForeignKey("characters.id", ondelete="RESTRICT")
    )
    target_character_id: Mapped[UUID] = mapped_column(
        ForeignKey("characters.id", ondelete="RESTRICT")
    )
    confidence: Mapped[float] = mapped_column(Float)
    description: Mapped[str] = mapped_column(Text)
    contract: Mapped[dict[str, Any]] = mapped_column(JSON)
    __table_args__ = (
        Index("uq_analysis_relationship_stable", "analysis_id", "stable_id", unique=True),
        CheckConstraint("confidence BETWEEN 0 AND 1", name="analysis_relationship_confidence"),
    )


class AnalysisBeatDependency(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "analysis_beat_dependencies"
    analysis_id: Mapped[UUID] = mapped_column(ForeignKey("episode_analyses.id", ondelete="CASCADE"))
    cause_beat_id: Mapped[UUID] = mapped_column(ForeignKey("plot_beats.id", ondelete="RESTRICT"))
    effect_beat_id: Mapped[UUID] = mapped_column(ForeignKey("plot_beats.id", ondelete="RESTRICT"))
    contract: Mapped[dict[str, Any]] = mapped_column(JSON)
    __table_args__ = (
        Index(
            "uq_analysis_beat_dependency",
            "analysis_id",
            "cause_beat_id",
            "effect_beat_id",
            unique=True,
        ),
        CheckConstraint("cause_beat_id <> effect_beat_id", name="analysis_dependency_not_self"),
    )
