"""Restartable relational projections for T20 semantic visual QA.

Canonical QA reports, sampled frames, contact sheets and sampling manifests live
in content-addressed assets. These tables hold the restartable checkpoints, the
stable QA identity that makes a retry free, and the constraints that keep the
invariants true in the database rather than only in Python: a non-pass result
always carries repair codes, a hard failure always agrees with its outcome, and
exactly one canonical result exists per QA run.

T23 owns provider attempts, budgets, pricing, telemetry and the cost ledger.
``visual_qa_attempts`` references a ``provider_attempts`` row; it never
duplicates one.
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

TARGET_TYPES = "('keyframe','video')"
OUTCOMES = "('PASS','FAIL','REVIEW')"
ATTEMPT_TYPES = "('first_pass','adjudication')"


class VisualQARun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One restartable QA run for exactly one target asset of one shot."""

    __tablename__ = "visual_qa_runs"
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    storyboard_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("storyboard_runs.id", ondelete="RESTRICT")
    )
    shot_id: Mapped[UUID] = mapped_column(ForeignKey("storyboard_shots.id", ondelete="CASCADE"))
    shot_workflow_identity: Mapped[str] = mapped_column(String(64))
    target_type: Mapped[str] = mapped_column(String(16))
    target_asset_id: Mapped[UUID] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    target_asset_sha256: Mapped[str] = mapped_column(String(64))
    qa_identity: Mapped[str] = mapped_column(String(64), unique=True)
    input_hash: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(64))
    importance: Mapped[str] = mapped_column(String(16))
    rubric_version: Mapped[str] = mapped_column(String(64))
    sampling_version: Mapped[str] = mapped_column(String(64))
    threshold_version: Mapped[str] = mapped_column(String(64))
    deterministic_version: Mapped[str] = mapped_column(String(64))
    pipeline_version: Mapped[str] = mapped_column(String(64))
    contact_sheet_asset_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT")
    )
    sampling_manifest_asset_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT")
    )
    report_asset_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT")
    )
    deterministic_report: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    selected_result_id: Mapped[UUID | None] = mapped_column(
        ForeignKey(
            "visual_qa_results.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_visual_qa_runs_selected_result",
        ),
        nullable=True,
    )
    final_outcome: Mapped[str | None] = mapped_column(String(16))
    final_score: Mapped[float | None] = mapped_column(Float)
    pass_threshold: Mapped[float | None] = mapped_column(Float)
    hard_failure: Mapped[bool] = mapped_column(Boolean, default=False)
    repair_recommendation: Mapped[str | None] = mapped_column(String(32))
    repair_codes: Mapped[list[str]] = mapped_column(JSON, default=list)
    warning_codes: Mapped[list[str]] = mapped_column(JSON, default=list)
    cost_microusd: Mapped[int] = mapped_column(BigInteger, default=0)
    error_code: Mapped[str | None] = mapped_column(String(128))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint(
            "project_id", "idempotency_key", "target_type", name="uq_visual_qa_run_idempotency"
        ),
        Index("ix_visual_qa_runs_shot_target", "shot_id", "target_type"),
        CheckConstraint(f"target_type IN {TARGET_TYPES}", name="visual_qa_run_target_type"),
        CheckConstraint(
            f"final_outcome IS NULL OR final_outcome IN {OUTCOMES}",
            name="visual_qa_run_outcome",
        ),
        CheckConstraint("length(qa_identity) = 64", name="visual_qa_run_identity_length"),
        CheckConstraint("length(input_hash) = 64", name="visual_qa_run_input_hash_length"),
        CheckConstraint(
            "length(target_asset_sha256) = 64", name="visual_qa_run_target_hash_length"
        ),
        CheckConstraint(
            "importance IN ('utility','normal','hero')", name="visual_qa_run_importance"
        ),
        CheckConstraint(
            "final_score IS NULL OR (final_score >= 0 AND final_score <= 100)",
            name="visual_qa_run_score_range",
        ),
        CheckConstraint(
            "pass_threshold IS NULL OR (pass_threshold >= 0 AND pass_threshold <= 100)",
            name="visual_qa_run_threshold_range",
        ),
        CheckConstraint("cost_microusd >= 0", name="visual_qa_run_nonnegative_cost"),
        # A hard failure can never be recorded as anything but FAIL.
        CheckConstraint(
            "NOT hard_failure OR final_outcome = 'FAIL'",
            name="visual_qa_run_hard_failure_blocks",
        ),
    )


class VisualQASampleRecord(UUIDPrimaryKeyMixin, Base):
    """One deterministically selected, decoded and hashed frame."""

    __tablename__ = "visual_qa_samples"
    qa_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("visual_qa_runs.id", ondelete="CASCADE"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer)
    sample_type: Mapped[str] = mapped_column(String(32))
    requested_timestamp_us: Mapped[int] = mapped_column(BigInteger)
    actual_timestamp_us: Mapped[int] = mapped_column(BigInteger)
    shot_relative_timestamp_us: Mapped[int] = mapped_column(BigInteger)
    frame_asset_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT")
    )
    frame_sha256: Mapped[str] = mapped_column(String(64))
    source_asset_id: Mapped[UUID] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    selection_reason: Mapped[str] = mapped_column(String(255))
    contact_sheet_position: Mapped[int | None] = mapped_column(Integer)
    measurements: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        UniqueConstraint("qa_run_id", "sequence", name="uq_visual_qa_sample_sequence"),
        UniqueConstraint("qa_run_id", "actual_timestamp_us", name="uq_visual_qa_sample_timestamp"),
        CheckConstraint(
            "sequence >= 0 AND requested_timestamp_us >= 0 AND actual_timestamp_us >= 0 "
            "AND shot_relative_timestamp_us >= 0",
            name="visual_qa_sample_nonnegative",
        ),
        CheckConstraint("length(frame_sha256) = 64", name="visual_qa_sample_hash_length"),
    )


class VisualQAAttempt(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One first-pass or adjudication evaluation of a QA run."""

    __tablename__ = "visual_qa_attempts"
    qa_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("visual_qa_runs.id", ondelete="CASCADE"), index=True
    )
    attempt_number: Mapped[int] = mapped_column(Integer)
    attempt_type: Mapped[str] = mapped_column(String(16))
    attempt_identity: Mapped[str] = mapped_column(String(64), unique=True)
    provider_attempt_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("provider_attempts.id", ondelete="RESTRICT")
    )
    provider: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(128))
    provider_request_id: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32))
    result_projection: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    failure_class: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(128))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint(
            "qa_run_id", "attempt_type", "attempt_number", name="uq_visual_qa_attempt_number"
        ),
        CheckConstraint(f"attempt_type IN {ATTEMPT_TYPES}", name="visual_qa_attempt_type"),
        CheckConstraint("attempt_number >= 1", name="visual_qa_attempt_positive_number"),
        CheckConstraint("length(attempt_identity) = 64", name="visual_qa_attempt_identity_length"),
    )


class VisualQAResultRecord(UUIDPrimaryKeyMixin, Base):
    """One recomputed, adjudicable outcome produced from one attempt."""

    __tablename__ = "visual_qa_results"
    qa_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("visual_qa_runs.id", ondelete="CASCADE"), index=True
    )
    attempt_id: Mapped[UUID] = mapped_column(
        ForeignKey("visual_qa_attempts.id", ondelete="CASCADE")
    )
    outcome: Mapped[str] = mapped_column(String(16))
    recomputed_score: Mapped[float] = mapped_column(Float)
    pass_threshold: Mapped[float] = mapped_column(Float)
    dimension_results: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    hard_failure: Mapped[bool] = mapped_column(Boolean, default=False)
    hard_failure_codes: Mapped[list[str]] = mapped_column(JSON, default=list)
    warning_codes: Mapped[list[str]] = mapped_column(JSON, default=list)
    repair_codes: Mapped[list[str]] = mapped_column(JSON, default=list)
    repair_recommendation: Mapped[str] = mapped_column(String(32))
    confidence: Mapped[float] = mapped_column(Float)
    adjudication: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    canonical: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        UniqueConstraint("attempt_id", name="uq_visual_qa_result_attempt"),
        # Exactly one canonical result per QA run, enforced by the database.
        Index(
            "uq_visual_qa_result_canonical",
            "qa_run_id",
            unique=True,
            sqlite_where=text("canonical = 1"),
            postgresql_where=text("canonical"),
        ),
        CheckConstraint(f"outcome IN {OUTCOMES}", name="visual_qa_result_outcome"),
        CheckConstraint(
            "recomputed_score >= 0 AND recomputed_score <= 100", name="visual_qa_result_score_range"
        ),
        CheckConstraint(
            "pass_threshold >= 0 AND pass_threshold <= 100",
            name="visual_qa_result_threshold_range",
        ),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="visual_qa_result_confidence"),
        # A non-pass outcome must carry repair codes, and a hard failure must FAIL.
        CheckConstraint(
            "outcome = 'PASS' OR json_array_length(repair_codes) > 0",
            name="visual_qa_result_requires_repair_codes",
        ),
        CheckConstraint(
            "NOT hard_failure OR outcome = 'FAIL'", name="visual_qa_result_hard_failure_blocks"
        ),
        CheckConstraint(
            "hard_failure = (json_array_length(hard_failure_codes) > 0)",
            name="visual_qa_result_hard_failure_consistency",
        ),
    )


class VisualQAEvidenceRecord(UUIDPrimaryKeyMixin, Base):
    """One concrete pointer proving what a finding is based on."""

    __tablename__ = "visual_qa_evidence"
    qa_result_id: Mapped[UUID] = mapped_column(
        ForeignKey("visual_qa_results.id", ondelete="CASCADE"), index=True
    )
    finding_id: Mapped[UUID] = mapped_column(nullable=False)
    sample_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("visual_qa_samples.id", ondelete="SET NULL")
    )
    frame_asset_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT")
    )
    shot_relative_timestamp_us: Mapped[int | None] = mapped_column(BigInteger)
    source_relative_timestamp_us: Mapped[int | None] = mapped_column(BigInteger)
    bounding_box: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    compared_reference_asset_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT")
    )
    evidence_type: Mapped[str] = mapped_column(String(32))
    confidence: Mapped[float] = mapped_column(Float)
    explanation: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        Index("ix_visual_qa_evidence_finding", "qa_result_id", "finding_id"),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1", name="visual_qa_evidence_confidence"
        ),
        CheckConstraint(
            "shot_relative_timestamp_us IS NULL OR shot_relative_timestamp_us >= 0",
            name="visual_qa_evidence_nonnegative_shot_timestamp",
        ),
        CheckConstraint(
            "source_relative_timestamp_us IS NULL OR source_relative_timestamp_us >= 0",
            name="visual_qa_evidence_nonnegative_source_timestamp",
        ),
        # Frame-located evidence must carry the timestamp that locates it.
        CheckConstraint(
            "evidence_type = 'whole_file' OR source_relative_timestamp_us IS NOT NULL",
            name="visual_qa_evidence_requires_timestamp",
        ),
    )


class VisualQAHumanReview(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One owner-scoped human resolution of an ambiguous ``REVIEW`` outcome."""

    __tablename__ = "visual_qa_human_reviews"
    qa_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("visual_qa_runs.id", ondelete="CASCADE"), index=True
    )
    expected_row_version: Mapped[int] = mapped_column(Integer)
    reviewer_principal: Mapped[str] = mapped_column(String(255))
    decision: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(String(500), default="")
    idempotency_key: Mapped[str] = mapped_column(String(255))
    __table_args__ = (
        UniqueConstraint(
            "qa_run_id", "idempotency_key", name="uq_visual_qa_human_review_idempotency"
        ),
        CheckConstraint(
            "decision IN ('approved','rejected')", name="visual_qa_human_review_decision"
        ),
        CheckConstraint("expected_row_version > 0", name="visual_qa_human_review_row_version"),
    )
