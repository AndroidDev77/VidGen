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
    UniqueConstraint,
)
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Mapped, mapped_column

from vidgen.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class SubtitleRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "subtitle_runs"

    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_video_id: Mapped[UUID] = mapped_column(
        ForeignKey("source_videos.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_audio_asset_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT"), index=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    acquisition_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(128))
    parameters: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    selected_candidate_id: Mapped[str | None] = mapped_column(String(255))
    quality_score: Mapped[float | None] = mapped_column(Float)
    coverage_score: Mapped[float | None] = mapped_column(Float)
    selected: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(128))

    __table_args__ = (
        UniqueConstraint("project_id", "idempotency_key", name="uq_sr_project_key"),
        CheckConstraint(
            "quality_score IS NULL OR quality_score BETWEEN 0 AND 1",
            name="quality_score_range",
        ),
        CheckConstraint(
            "coverage_score IS NULL OR coverage_score BETWEEN 0 AND 1",
            name="coverage_score_range",
        ),
        Index(
            "uq_subtitle_runs_selected_project",
            "project_id",
            unique=True,
            postgresql_where=sql_text("selected"),
            sqlite_where=sql_text("selected = 1"),
        ),
    )


class SubtitleCandidateRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "subtitle_candidates"

    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("subtitle_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    candidate_id: Mapped[str] = mapped_column(String(255), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    provider: Mapped[str] = mapped_column(String(128), nullable=False)
    provider_subtitle_id: Mapped[str | None] = mapped_column(String(255))
    provider_file_id: Mapped[int | None] = mapped_column(Integer)
    asset_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT"), index=True
    )
    stream_index: Mapped[int | None] = mapped_column(Integer)
    language: Mapped[str | None] = mapped_column(String(32))
    subtitle_format: Mapped[str] = mapped_column(String(32), nullable=False)
    score: Mapped[float | None] = mapped_column(Float)
    selected: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_request_id: Mapped[str | None] = mapped_column(String(255))
    provider_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    quality: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(128))

    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_sc_run_sequence"),
        UniqueConstraint("run_id", "candidate_id", name="uq_sc_run_candidate"),
        CheckConstraint("sequence >= 0", name="sequence_nonnegative"),
        CheckConstraint("stream_index IS NULL OR stream_index >= 0", name="stream_nonnegative"),
        CheckConstraint("score IS NULL OR score BETWEEN 0 AND 1", name="score_range"),
        Index(
            "uq_subtitle_candidates_selected_run",
            "run_id",
            unique=True,
            postgresql_where=sql_text("selected"),
            sqlite_where=sql_text("selected = 1"),
        ),
    )
