from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Mapped, mapped_column

import vidgen.db.subtitle_models  # noqa: F401
from vidgen.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class TranscriptionRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "transcription_runs"

    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_video_id: Mapped[UUID] = mapped_column(
        ForeignKey("source_videos.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_audio_asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    language: Mapped[str | None] = mapped_column(String(32))
    chunker_version: Mapped[str] = mapped_column(String(32), nullable=False)
    provider: Mapped[str] = mapped_column(String(128), nullable=False)
    transcription_model: Mapped[str] = mapped_column(String(128), nullable=False)
    diarization_model: Mapped[str] = mapped_column(String(128), nullable=False)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    coverage_score: Mapped[float | None] = mapped_column(Float)
    selected: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(128))

    __table_args__ = (
        UniqueConstraint("project_id", "idempotency_key"),
        CheckConstraint(
            "coverage_score IS NULL OR coverage_score BETWEEN 0 AND 1",
            name="coverage_score_range",
        ),
        Index(
            "uq_transcription_runs_selected_project",
            "project_id",
            unique=True,
            postgresql_where=sql_text("selected"),
            sqlite_where=sql_text("selected = 1"),
        ),
    )


class TranscriptionChunk(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "transcription_chunks"

    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("transcription_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT"), nullable=False
    )
    source_start_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    source_end_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    overlap_before_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    overlap_after_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    provider_request_id: Mapped[str | None] = mapped_column(String(255))
    diarization_request_id: Mapped[str | None] = mapped_column(String(255))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    provider_result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    diarization_result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(128))

    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_tc_run_sequence"),
        UniqueConstraint("run_id", "chunk_asset_id", name="uq_tc_run_asset"),
        UniqueConstraint("provider_request_id", name="uq_tc_provider_request"),
        UniqueConstraint("diarization_request_id", name="uq_tc_diarization_request"),
        CheckConstraint("sequence >= 0", name="sequence_nonnegative"),
        CheckConstraint(
            "source_end_seconds > source_start_seconds", name="source_interval_positive"
        ),
        CheckConstraint("overlap_before_seconds >= 0", name="overlap_before_nonnegative"),
        CheckConstraint("overlap_after_seconds >= 0", name="overlap_after_nonnegative"),
        CheckConstraint("byte_size > 0", name="byte_size_positive"),
        CheckConstraint("length(sha256) = 64", name="sha256_length"),
        CheckConstraint("attempt_count >= 0", name="attempt_count_nonnegative"),
    )


class Transcript(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "transcripts"

    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("transcription_runs.id", ondelete="CASCADE"), nullable=True, unique=True
    )
    subtitle_run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("subtitle_runs.id", ondelete="CASCADE"), nullable=True, unique=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    language: Mapped[str | None] = mapped_column(String(32))
    text: Mapped[str] = mapped_column(Text, nullable=False)
    transcript_asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT"), nullable=False
    )
    duration_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    coverage_score: Mapped[float] = mapped_column(Float, nullable=False)
    selected: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    warnings: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)

    __table_args__ = (
        UniqueConstraint("project_id", "version"),
        UniqueConstraint("transcript_asset_id", name="uq_transcripts_asset_id"),
        CheckConstraint("version > 0", name="version_positive"),
        CheckConstraint("duration_seconds > 0", name="duration_positive"),
        CheckConstraint("coverage_score BETWEEN 0 AND 1", name="coverage_score_range"),
        CheckConstraint(
            "(run_id IS NOT NULL AND subtitle_run_id IS NULL) OR "
            "(run_id IS NULL AND subtitle_run_id IS NOT NULL)",
            name="exactly_one_origin_run",
        ),
        Index(
            "uq_transcripts_selected_project",
            "project_id",
            unique=True,
            postgresql_where=sql_text("selected"),
            sqlite_where=sql_text("selected = 1"),
        ),
    )


class TranscriptSegmentRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "transcript_segments"

    transcript_id: Mapped[UUID] = mapped_column(
        ForeignKey("transcripts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    start_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    end_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    speaker_label: Mapped[str | None] = mapped_column(String(64))
    confidence: Mapped[float | None] = mapped_column(Float)
    source_chunk_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    words: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    provenance: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    __table_args__ = (
        UniqueConstraint("transcript_id", "sequence", name="uq_ts_transcript_sequence"),
        CheckConstraint("sequence >= 0", name="sequence_nonnegative"),
        CheckConstraint("end_seconds > start_seconds", name="interval_positive"),
        CheckConstraint(
            "confidence IS NULL OR confidence BETWEEN 0 AND 1", name="confidence_range"
        ),
    )


class SpeakerTurnRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "speaker_turns"

    transcript_id: Mapped[UUID] = mapped_column(
        ForeignKey("transcripts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    speaker_label: Mapped[str] = mapped_column(String(64), nullable=False)
    start_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    end_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float)
    source_chunk_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    provider_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    alternate_mappings: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    warnings: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)

    __table_args__ = (
        UniqueConstraint("transcript_id", "sequence", name="uq_st_transcript_sequence"),
        CheckConstraint("sequence >= 0", name="sequence_nonnegative"),
        CheckConstraint("end_seconds > start_seconds", name="interval_positive"),
        CheckConstraint(
            "confidence IS NULL OR confidence BETWEEN 0 AND 1", name="confidence_range"
        ),
    )
