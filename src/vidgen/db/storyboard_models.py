"""T13 storyboard relational projections.

Canonical storyboard and timing-manifest payloads live in content-addressed
assets. These tables hold restartable checkpoints, the exact integer timing
projection, and the constraints that keep a run idempotent. T23 provider-attempt,
budget, pricing, telemetry, and ledger tables are reused, never duplicated.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    BigInteger,
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


class StoryboardRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "storyboard_runs"
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    episode_model_id: Mapped[UUID] = mapped_column(
        ForeignKey("episode_analyses.id", ondelete="RESTRICT")
    )
    script_id: Mapped[UUID] = mapped_column(ForeignKey("scripts.id", ondelete="RESTRICT"))
    script_version: Mapped[int] = mapped_column(Integer)
    narration_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("narration_runs.id", ondelete="RESTRICT")
    )
    capability_profile_id: Mapped[str] = mapped_column(String(128))
    capability_hash: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(255))
    input_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(64))
    provider: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(128))
    contract_version: Mapped[str] = mapped_column(String(32))
    director_version: Mapped[str] = mapped_column(String(32))
    prompt_version: Mapped[str] = mapped_column(String(32))
    retimer_version: Mapped[str] = mapped_column(String(32))
    version: Mapped[int] = mapped_column(Integer, default=1)
    selected: Mapped[bool] = mapped_column(Boolean, default=False)
    storyboard_asset_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT")
    )
    timing_manifest_asset_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT")
    )
    validation_report_asset_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT")
    )
    segment_count: Mapped[int] = mapped_column(Integer, default=0)
    shot_count: Mapped[int] = mapped_column(Integer, default=0)
    total_duration_us: Mapped[int] = mapped_column(BigInteger, default=0)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(128))
    __table_args__ = (
        UniqueConstraint("project_id", "idempotency_key", name="uq_storyboard_run_idempotency"),
        # Exactly one selected storyboard per project and upstream lineage version.
        Index(
            "uq_storyboard_selected_upstream",
            "project_id",
            "script_id",
            "script_version",
            "narration_run_id",
            unique=True,
            sqlite_where=text("selected = 1"),
            postgresql_where=text("selected"),
        ),
        CheckConstraint("script_version > 0", name="storyboard_run_positive_script_version"),
        CheckConstraint("version > 0", name="storyboard_run_positive_version"),
        CheckConstraint("segment_count >= 0", name="storyboard_run_nonnegative_segments"),
        CheckConstraint("shot_count >= 0", name="storyboard_run_nonnegative_shots"),
        CheckConstraint("total_duration_us >= 0", name="storyboard_run_nonnegative_duration"),
        CheckConstraint("length(input_hash) = 64", name="storyboard_run_input_hash_length"),
        CheckConstraint("length(capability_hash) = 64", name="storyboard_run_capability_hash_len"),
    )


class StoryboardSegmentCheckpoint(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "storyboard_segment_checkpoints"
    storyboard_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("storyboard_runs.id", ondelete="CASCADE"), index=True
    )
    script_segment_id: Mapped[UUID] = mapped_column(
        ForeignKey("script_segments.id", ondelete="RESTRICT")
    )
    narration_segment_id: Mapped[UUID] = mapped_column(
        ForeignKey("narration_segments.id", ondelete="RESTRICT")
    )
    sequence: Mapped[int] = mapped_column(Integer)
    input_hash: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True)
    status: Mapped[str] = mapped_column(String(32))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    repair_attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    narration_duration_us: Mapped[int] = mapped_column(BigInteger)
    global_start_us: Mapped[int] = mapped_column(BigInteger, default=0)
    provider_request_id: Mapped[str | None] = mapped_column(String(255))
    provider_result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    validation_report: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    # Retimer adjustments and residuals for this segment, so a resumed run can
    # rebuild the whole timing manifest without re-solving completed segments.
    timing_adjustments: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    incoming_continuity: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    outgoing_continuity: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String(128))
    __table_args__ = (
        UniqueConstraint("storyboard_run_id", "sequence", name="uq_storyboard_checkpoint_sequence"),
        UniqueConstraint(
            "storyboard_run_id",
            "script_segment_id",
            name="uq_storyboard_checkpoint_script_segment",
        ),
        Index(
            "uq_storyboard_checkpoint_provider_request",
            "provider_request_id",
            unique=True,
            sqlite_where=text("provider_request_id IS NOT NULL"),
            postgresql_where=text("provider_request_id IS NOT NULL"),
        ),
        CheckConstraint("sequence >= 0", name="storyboard_checkpoint_nonnegative_sequence"),
        CheckConstraint("attempt_count >= 0", name="storyboard_checkpoint_attempt_range"),
        CheckConstraint(
            "repair_attempt_count >= 0 AND repair_attempt_count <= attempt_count",
            name="storyboard_checkpoint_repair_range",
        ),
        CheckConstraint(
            "narration_duration_us > 0", name="storyboard_checkpoint_positive_duration"
        ),
        CheckConstraint("global_start_us >= 0", name="storyboard_checkpoint_nonnegative_start"),
        CheckConstraint("length(input_hash) = 64", name="storyboard_checkpoint_hash_length"),
    )


class StoryboardShotRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "storyboard_shots"
    storyboard_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("storyboard_runs.id", ondelete="CASCADE"), index=True
    )
    segment_checkpoint_id: Mapped[UUID] = mapped_column(
        ForeignKey("storyboard_segment_checkpoints.id", ondelete="CASCADE")
    )
    # Content-derived identity, stable across restarts of the same run.
    stable_shot_id: Mapped[UUID] = mapped_column(nullable=False)
    global_sequence: Mapped[int] = mapped_column(Integer)
    segment_sequence: Mapped[int] = mapped_column(Integer)
    script_segment_id: Mapped[UUID] = mapped_column(
        ForeignKey("script_segments.id", ondelete="RESTRICT")
    )
    narration_segment_id: Mapped[UUID] = mapped_column(
        ForeignKey("narration_segments.id", ondelete="RESTRICT")
    )
    start_us: Mapped[int] = mapped_column(BigInteger)
    end_us: Mapped[int] = mapped_column(BigInteger)
    global_start_us: Mapped[int] = mapped_column(BigInteger)
    global_end_us: Mapped[int] = mapped_column(BigInteger)
    usable_duration_us: Mapped[int] = mapped_column(BigInteger)
    requested_generation_duration_us: Mapped[int] = mapped_column(BigInteger)
    trim_start_us: Mapped[int] = mapped_column(BigInteger, default=0)
    trim_end_us: Mapped[int] = mapped_column(BigInteger, default=0)
    transition_handle_us: Mapped[int] = mapped_column(BigInteger, default=0)
    word_start_index: Mapped[int] = mapped_column(Integer)
    word_end_index: Mapped[int] = mapped_column(Integer)
    camera: Mapped[dict[str, Any]] = mapped_column(JSON)
    action: Mapped[dict[str, Any]] = mapped_column(JSON)
    transition_in: Mapped[dict[str, Any]] = mapped_column(JSON)
    transition_out: Mapped[dict[str, Any]] = mapped_column(JSON)
    references: Mapped[dict[str, Any]] = mapped_column(JSON)
    incoming_continuity: Mapped[dict[str, Any]] = mapped_column(JSON)
    outgoing_continuity: Mapped[dict[str, Any]] = mapped_column(JSON)
    contract: Mapped[dict[str, Any]] = mapped_column(JSON)
    provenance: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    __table_args__ = (
        UniqueConstraint(
            "storyboard_run_id", "global_sequence", name="uq_storyboard_shot_global_sequence"
        ),
        UniqueConstraint(
            "segment_checkpoint_id",
            "segment_sequence",
            name="uq_storyboard_shot_segment_sequence",
        ),
        UniqueConstraint(
            "storyboard_run_id", "stable_shot_id", name="uq_storyboard_shot_stable_identity"
        ),
        CheckConstraint("global_sequence >= 0", name="storyboard_shot_nonnegative_global_sequence"),
        CheckConstraint(
            "segment_sequence >= 0", name="storyboard_shot_nonnegative_segment_sequence"
        ),
        CheckConstraint("usable_duration_us > 0", name="storyboard_shot_positive_duration"),
        CheckConstraint(
            "requested_generation_duration_us >= usable_duration_us",
            name="storyboard_shot_generation_covers_usable",
        ),
        CheckConstraint("end_us > start_us", name="storyboard_shot_end_after_start"),
        CheckConstraint(
            "global_end_us > global_start_us", name="storyboard_shot_global_end_after_start"
        ),
        CheckConstraint(
            "end_us - start_us = usable_duration_us", name="storyboard_shot_interval_matches"
        ),
        CheckConstraint(
            "trim_start_us >= 0 AND trim_end_us >= 0 AND transition_handle_us >= 0",
            name="storyboard_shot_nonnegative_trim",
        ),
        CheckConstraint(
            "trim_start_us + trim_end_us = requested_generation_duration_us - usable_duration_us",
            name="storyboard_shot_trim_accounts_for_generation",
        ),
        CheckConstraint(
            "word_end_index > word_start_index AND word_start_index >= 0",
            name="storyboard_shot_word_range",
        ),
    )


class StoryboardRepairAttempt(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "storyboard_repair_attempts"
    segment_checkpoint_id: Mapped[UUID] = mapped_column(
        ForeignKey("storyboard_segment_checkpoints.id", ondelete="CASCADE"), index=True
    )
    attempt_number: Mapped[int] = mapped_column(Integer)
    provider_attempt_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("provider_attempts.id", ondelete="SET NULL")
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True)
    input_diagnostics: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    provider_request_id: Mapped[str | None] = mapped_column(String(255))
    validation_result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32))
    error_code: Mapped[str | None] = mapped_column(String(128))
    completed_at: Mapped[datetime | None]
    __table_args__ = (
        UniqueConstraint(
            "segment_checkpoint_id", "attempt_number", name="uq_storyboard_repair_attempt_number"
        ),
        CheckConstraint(
            "attempt_number >= 1 AND attempt_number <= 3", name="storyboard_repair_attempt_range"
        ),
    )
