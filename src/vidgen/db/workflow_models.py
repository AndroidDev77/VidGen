from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import JSON, Boolean, CheckConstraint, ForeignKey, Index, Integer, String
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Mapped, mapped_column

from vidgen.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ProjectWorkflowRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "project_workflow_runs"
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    workflow_id: Mapped[str] = mapped_column(String(255), unique=True)
    run_id: Mapped[str] = mapped_column(String(255), unique=True)
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    failure: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    __table_args__ = (
        Index("uq_workflow_project_idempotency", "project_id", "idempotency_key", unique=True),
    )


class StageExecution(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "stage_executions"
    workflow_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("project_workflow_runs.id", ondelete="CASCADE")
    )
    stage: Mapped[str] = mapped_column(String(64), nullable=False)
    activity_id: Mapped[str] = mapped_column(String(255), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_asset_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT")
    )
    output_entity_id: Mapped[UUID | None]
    failure: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    __table_args__ = (
        Index("uq_stage_workflow_stage", "workflow_run_id", "stage", unique=True),
        Index("uq_stage_workflow_idempotency", "workflow_run_id", "idempotency_key", unique=True),
        CheckConstraint("attempt_count >= 0", name="nonnegative_attempt_count"),
    )


class EvidencePackageRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "evidence_packages"
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    selected: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    source_video_id: Mapped[UUID] = mapped_column(
        ForeignKey("source_videos.id", ondelete="RESTRICT")
    )
    source_video_asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT")
    )
    source_audio_asset_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT")
    )
    transcript_id: Mapped[UUID] = mapped_column(nullable=False)
    transcript_asset_id: Mapped[UUID] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    transcript_origin: Mapped[str] = mapped_column(String(32), nullable=False)
    subtitle_asset_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT")
    )
    contact_sheet_asset_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT")
    )
    provenance: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    __table_args__ = (
        Index("uq_evidence_project_version", "project_id", "version", unique=True),
        Index("uq_evidence_project_hash", "project_id", "input_hash", unique=True),
        Index(
            "uq_evidence_selected_project",
            "project_id",
            unique=True,
            postgresql_where=sql_text("selected"),
            sqlite_where=sql_text("selected = 1"),
        ),
        CheckConstraint("version > 0", name="positive_version"),
        CheckConstraint("length(input_hash) = 64", name="input_hash_length"),
    )


class SceneEvidenceRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "scene_evidence"
    evidence_package_id: Mapped[UUID] = mapped_column(
        ForeignKey("evidence_packages.id", ondelete="CASCADE")
    )
    scene_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    source_start_seconds: Mapped[float] = mapped_column(nullable=False)
    source_end_seconds: Mapped[float] = mapped_column(nullable=False)
    frame_asset_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    __table_args__ = (
        Index("uq_scene_evidence_sequence", "evidence_package_id", "scene_sequence", unique=True),
        CheckConstraint("source_end_seconds > source_start_seconds", name="valid_source_interval"),
        CheckConstraint("scene_sequence >= 0", name="nonnegative_scene_sequence"),
    )
