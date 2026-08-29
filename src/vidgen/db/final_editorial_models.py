"""Restartable relational projections for T22 final editorial QA.

The canonical report, measurement manifest, caption and audio reports, contact
sheet and adjudication all live in content-addressed assets. These tables hold
the restartable phase checkpoints, the stable identity that makes a retry free,
and the invariants that belong in the database rather than only in Python:

* one final-QA run per project idempotency key,
* one selected current report per render identity,
* no ``PASS`` recorded alongside a blocking finding or an unresolved review,
* no completion gate referencing a render other than the one its run inspected,
* a human review that always carries a structured reason and the row version it
  was decided against,
* nonnegative measurements and counts throughout.

T23 owns provider attempts, budgets, pricing, telemetry and the cost ledger, and
T17, T20 and T21 own renders, shot QA and repairs. Every reference here points
at those tables; none of them is duplicated.
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
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from vidgen.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

STATUSES = (
    "('FINAL_QA_QUEUED','FINAL_QA_VALIDATING_INPUTS','FINAL_QA_CHECKING_MEDIA',"
    "'FINAL_QA_CHECKING_CAPTIONS','FINAL_QA_ANALYZING','FINAL_QA_ADJUDICATING',"
    "'FINAL_QA_REVIEW_REQUIRED','FINAL_QA_PASSED','FINAL_QA_FAILED')"
)
PHASES = (
    "('INPUT_VALIDATION','DETERMINISTIC_MEDIA_QA','CAPTION_QA','EDITORIAL_ANALYSIS',"
    "'ADJUDICATION','COMPLETION_GATE')"
)
DECISIONS = "('PASS','FAIL','REVIEW')"
CHECK_TYPES = "('lineage','media','timeline','audio','caption','manifest')"
CHECK_STATUSES = "('pass','fail','warning','not_applicable')"
ATTEMPT_PHASES = "('EDITORIAL_ANALYSIS','ADJUDICATION')"
REVIEW_DECISIONS = "('accept','reject','escalate')"


class FinalEditorialRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One restartable final-QA run bound to exactly one T17 render identity."""

    __tablename__ = "final_editorial_runs"
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    render_job_id: Mapped[UUID] = mapped_column(ForeignKey("render_jobs.id", ondelete="RESTRICT"))
    final_render_asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT")
    )
    render_manifest_asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT")
    )
    render_identity: Mapped[str] = mapped_column(String(64), index=True)
    final_qa_identity: Mapped[str] = mapped_column(String(64), unique=True)
    input_hash: Mapped[str] = mapped_column(String(64))
    configuration_hash: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(48))
    current_phase: Mapped[str] = mapped_column(String(32))
    completed_phases: Mapped[list[str]] = mapped_column(JSON, default=list)
    selected: Mapped[bool] = mapped_column(Boolean, default=False)
    final_decision: Mapped[str | None] = mapped_column(String(16))
    report_asset_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT")
    )
    measurement_asset_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT")
    )
    audio_report_asset_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT")
    )
    caption_report_asset_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT")
    )
    contact_sheet_asset_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT")
    )
    adjudication_asset_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT")
    )
    pipeline_version: Mapped[str] = mapped_column(String(64))
    gate_version: Mapped[str] = mapped_column(String(64))
    blocking_finding_count: Mapped[int] = mapped_column(Integer, default=0)
    review_finding_count: Mapped[int] = mapped_column(Integer, default=0)
    warning_finding_count: Mapped[int] = mapped_column(Integer, default=0)
    deterministic_failure_count: Mapped[int] = mapped_column(Integer, default=0)
    remediation_targets: Mapped[list[str]] = mapped_column(JSON, default=list)
    cost_microusd: Mapped[int] = mapped_column(BigInteger, default=0)
    first_pass_provider: Mapped[str | None] = mapped_column(String(64))
    first_pass_model: Mapped[str | None] = mapped_column(String(128))
    error_code: Mapped[str | None] = mapped_column(String(128))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint(
            "project_id", "idempotency_key", name="uq_final_editorial_run_idempotency"
        ),
        # Exactly one selected current report per render identity: a second
        # selected run for the same render can never exist.
        Index(
            "uq_final_editorial_selected_render",
            "final_render_asset_id",
            unique=True,
            postgresql_where=text("selected"),
            sqlite_where=text("selected"),
        ),
        Index("ix_final_editorial_runs_project_status", "project_id", "status"),
        CheckConstraint(f"status IN {STATUSES}", name="final_editorial_run_status"),
        CheckConstraint(f"current_phase IN {PHASES}", name="final_editorial_run_phase"),
        CheckConstraint(
            f"final_decision IS NULL OR final_decision IN {DECISIONS}",
            name="final_editorial_run_decision",
        ),
        CheckConstraint("length(final_qa_identity) = 64", name="final_editorial_run_identity_len"),
        CheckConstraint("length(input_hash) = 64", name="final_editorial_run_input_hash_len"),
        CheckConstraint(
            "length(configuration_hash) = 64", name="final_editorial_run_config_hash_len"
        ),
        CheckConstraint("length(render_identity) = 64", name="final_editorial_run_render_id_len"),
        CheckConstraint(
            "blocking_finding_count >= 0 AND review_finding_count >= 0 "
            "AND warning_finding_count >= 0 AND deterministic_failure_count >= 0 "
            "AND cost_microusd >= 0",
            name="final_editorial_run_nonnegative",
        ),
        # A PASS can never coexist with a blocking issue or an unresolved review.
        CheckConstraint(
            "final_decision <> 'PASS' OR (blocking_finding_count = 0 "
            "AND deterministic_failure_count = 0 AND review_finding_count = 0)",
            name="final_editorial_run_pass_is_clean",
        ),
        # A FAIL always names at least one confirmed blocking issue.
        CheckConstraint(
            "final_decision <> 'FAIL' OR blocking_finding_count > 0 "
            "OR deterministic_failure_count > 0",
            name="final_editorial_run_fail_has_cause",
        ),
        # Only a decided run may be selected, and only with a persisted report.
        CheckConstraint(
            "NOT selected OR (final_decision IS NOT NULL AND report_asset_id IS NOT NULL)",
            name="final_editorial_run_selection_requires_report",
        ),
    )


class FinalEditorialCheckRecord(UUIDPrimaryKeyMixin, Base):
    """One persisted deterministic, audio or caption check with its measurement."""

    __tablename__ = "final_editorial_checks"
    final_editorial_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("final_editorial_runs.id", ondelete="CASCADE"), index=True
    )
    check_key: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    check_type: Mapped[str] = mapped_column(String(16))
    check_code: Mapped[str] = mapped_column(String(64))
    check_version: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    blocking: Mapped[bool] = mapped_column(Boolean, default=False)
    measurements: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    thresholds: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    findings: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    evidence_references: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    #: Denormalized so "a blocking check carries evidence" is a database
    #: constraint rather than a convention, portably across dialects.
    evidence_count: Mapped[int] = mapped_column(Integer, default=0)
    start_us: Mapped[int | None] = mapped_column(BigInteger)
    end_us: Mapped[int | None] = mapped_column(BigInteger)
    tool: Mapped[str] = mapped_column(String(64), default="")
    tool_version: Mapped[str] = mapped_column(String(128), default="")
    message: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint(
            "final_editorial_run_id", "check_key", name="uq_final_editorial_check_identity"
        ),
        CheckConstraint(f"check_type IN {CHECK_TYPES}", name="final_editorial_check_type"),
        CheckConstraint(f"status IN {CHECK_STATUSES}", name="final_editorial_check_status"),
        # A failed check is always blocking, and only a failed check may block.
        CheckConstraint(
            "(status = 'fail') = blocking", name="final_editorial_check_failure_blocks"
        ),
        CheckConstraint(
            "start_us IS NULL OR start_us >= 0", name="final_editorial_check_nonnegative_start"
        ),
        CheckConstraint(
            "end_us IS NULL OR start_us IS NULL OR end_us >= start_us",
            name="final_editorial_check_ordered_range",
        ),
        CheckConstraint("evidence_count >= 0", name="final_editorial_check_nonnegative_evidence"),
        # A blocking check must carry the evidence a remediation route needs.
        CheckConstraint(
            "NOT blocking OR evidence_count > 0", name="final_editorial_check_evidence_required"
        ),
    )


class FinalEditorialProviderAttempt(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One first-pass or adjudication evaluation of a final-QA run.

    The durable provider identity, its pricing, reservation and ledger entries
    all live in the T23 tables; this row projects the reference and the bounded
    result the pipeline needs to resume without a second paid call.
    """

    __tablename__ = "final_editorial_provider_attempts"
    final_editorial_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("final_editorial_runs.id", ondelete="CASCADE"), index=True
    )
    phase: Mapped[str] = mapped_column(String(32))
    attempt_number: Mapped[int] = mapped_column(Integer, default=1)
    attempt_identity: Mapped[str] = mapped_column(String(64), unique=True)
    provider_attempt_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("provider_attempts.id", ondelete="RESTRICT")
    )
    provider: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(128))
    provider_request_id: Mapped[str | None] = mapped_column(String(255))
    input_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))
    result_projection: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    failure_class: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(128))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint(
            "final_editorial_run_id",
            "phase",
            "attempt_number",
            name="uq_final_editorial_attempt_number",
        ),
        CheckConstraint(f"phase IN {ATTEMPT_PHASES}", name="final_editorial_attempt_phase"),
        CheckConstraint("attempt_number >= 1", name="final_editorial_attempt_positive"),
        CheckConstraint(
            "length(attempt_identity) = 64", name="final_editorial_attempt_identity_len"
        ),
        CheckConstraint("length(input_hash) = 64", name="final_editorial_attempt_hash_len"),
    )


class FinalEditorialReview(UUIDPrimaryKeyMixin, Base):
    """One human adjudication of one genuinely uncertain semantic finding.

    A reviewer may never resolve a deterministic hard failure or a stale lineage;
    the service enforces that, and ``expected_row_version`` makes a stale decision
    lose rather than silently overwrite a newer one.
    """

    __tablename__ = "final_editorial_reviews"
    final_editorial_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("final_editorial_runs.id", ondelete="CASCADE"), index=True
    )
    finding_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    reviewer_subject: Mapped[str] = mapped_column(String(255))
    decision: Mapped[str] = mapped_column(String(16))
    reason_code: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(String(1000))
    expected_row_version: Mapped[int] = mapped_column(Integer)
    idempotency_key: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        UniqueConstraint(
            "final_editorial_run_id", "finding_id", name="uq_final_editorial_review_finding"
        ),
        CheckConstraint(f"decision IN {REVIEW_DECISIONS}", name="final_editorial_review_decision"),
        CheckConstraint("length(reason) > 0", name="final_editorial_review_requires_reason"),
        CheckConstraint(
            "expected_row_version >= 0", name="final_editorial_review_nonnegative_version"
        ),
    )


class FinalCompletionGate(UUIDPrimaryKeyMixin, Base):
    """The immutable completion decision for one project and render identity."""

    __tablename__ = "final_completion_gates"
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    final_editorial_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("final_editorial_runs.id", ondelete="CASCADE")
    )
    final_render_asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT")
    )
    render_identity: Mapped[str] = mapped_column(String(64))
    decision: Mapped[str] = mapped_column(String(16))
    blocking_finding_count: Mapped[int] = mapped_column(Integer, default=0)
    review_finding_count: Mapped[int] = mapped_column(Integer, default=0)
    deterministic_failure_count: Mapped[int] = mapped_column(Integer, default=0)
    gate_version: Mapped[str] = mapped_column(String(64))
    reasons: Mapped[list[str]] = mapped_column(JSON, default=list)
    duration_us: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        UniqueConstraint(
            "final_editorial_run_id", "gate_version", name="uq_final_completion_gate_run"
        ),
        Index("ix_final_completion_gates_project_decision", "project_id", "decision"),
        CheckConstraint(f"decision IN {DECISIONS}", name="final_completion_gate_decision"),
        CheckConstraint("length(render_identity) = 64", name="final_completion_gate_render_len"),
        CheckConstraint(
            "blocking_finding_count >= 0 AND review_finding_count >= 0 "
            "AND deterministic_failure_count >= 0",
            name="final_completion_gate_nonnegative",
        ),
        # A PASS gate can never be recorded over an unresolved blocker.
        CheckConstraint(
            "decision <> 'PASS' OR (blocking_finding_count = 0 "
            "AND deterministic_failure_count = 0 AND review_finding_count = 0)",
            name="final_completion_gate_pass_is_clean",
        ),
        CheckConstraint(
            "duration_us IS NULL OR duration_us >= 0",
            name="final_completion_gate_nonnegative_duration",
        ),
    )
