"""Restartable relational projections for T21 repair and fallback routing.

Canonical repair plans, prompt deltas and fallback manifests are small enough to
project inline; the rendered media itself lives in content-addressed assets.
These tables hold the restartable checkpoints, the stable identities that make a
retry free, and the invariants that belong in the database rather than only in
Python:

* one repair run per project idempotency key,
* dense, unique attempt ordinals bounded by the policy,
* a predecessor that exists, is not the attempt itself, and is absent only for
  the root attempt,
* at most one selected attempt per shot, and never one without a T20 result,
* a ``HUMAN_REVIEW_REQUIRED`` run that always carries a structured reason.

T23 owns provider attempts, budgets, pricing and the cost ledger, and T20 owns
QA runs and results. Every reference here points at those tables; none of them
is duplicated.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from vidgen.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

Money = Numeric(18, 6)

RUN_STATES = (
    "('REPAIR_PLANNING','REPAIRING','ALTERNATE_PROVIDER','FALLBACK_RENDERING',"
    "'REVALIDATING','HUMAN_REVIEW_REQUIRED','LOCKED','REPAIR_FAILED')"
)
ATTEMPT_KINDS = "('original','same_provider_repair','alternate_provider','deterministic_fallback')"
ATTEMPT_STATUSES = (
    "('planned','submitted','polling','downloading','revalidating','passed','failed','cancelled')"
)
FAILURE_CATEGORIES = (
    "('prompt_issue','reference_issue','seed_issue','provider_issue','impossible_shot')"
)
#: One original generation, two same-provider repairs, one alternate provider
#: attempt and one deterministic fallback render.
MAX_ATTEMPT_ORDINAL = 4
MAX_TOTAL_ATTEMPTS = MAX_ATTEMPT_ORDINAL + 1


class RepairRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One bounded, restartable repair of exactly one failed shot."""

    __tablename__ = "repair_runs"
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    shot_id: Mapped[UUID] = mapped_column(ForeignKey("storyboard_shots.id", ondelete="CASCADE"))
    root_animation_attempt_id: Mapped[UUID] = mapped_column(
        ForeignKey("animation_generated_videos.id", ondelete="RESTRICT")
    )
    triggering_qa_result_id: Mapped[UUID] = mapped_column(
        ForeignKey("visual_qa_results.id", ondelete="RESTRICT")
    )
    policy_version: Mapped[str] = mapped_column(String(64))
    policy: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    classifier_version: Mapped[str] = mapped_column(String(64))
    planner_version: Mapped[str] = mapped_column(String(64))
    input_hash: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(255))
    state: Mapped[str] = mapped_column(String(32))
    classification: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    selected_attempt_id: Mapped[UUID | None] = mapped_column(
        ForeignKey(
            "repair_attempts.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_repair_runs_selected_attempt",
        ),
        nullable=True,
    )
    selected_asset_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT")
    )
    final_qa_result_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("visual_qa_results.id", ondelete="RESTRICT")
    )
    final_qa_score: Mapped[float | None] = mapped_column(Numeric(6, 3))
    total_attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    same_provider_repairs_used: Mapped[int] = mapped_column(Integer, default=0)
    alternate_provider_attempts_used: Mapped[int] = mapped_column(Integer, default=0)
    fallback_renders_used: Mapped[int] = mapped_column(Integer, default=0)
    total_repair_cost: Mapped[Decimal] = mapped_column(Money, default=0)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    human_review_reason: Mapped[str | None] = mapped_column(String(64))
    human_review_resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancellation_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    error_code: Mapped[str | None] = mapped_column(String(128))
    #: Optimistic advance token. Two workers that read the same value race on the
    #: conditional update, and exactly one wins, so a repair run never advances
    #: twice for one decision.
    advance_token: Mapped[int] = mapped_column(Integer, default=1)
    __table_args__ = (
        UniqueConstraint("project_id", "idempotency_key", name="uq_repair_run_idempotency"),
        Index("ix_repair_runs_shot_state", "shot_id", "state"),
        CheckConstraint(f"state IN {RUN_STATES}", name="repair_run_state"),
        CheckConstraint("length(input_hash) = 64", name="repair_run_input_hash_length"),
        CheckConstraint(
            f"total_attempt_count >= 0 AND total_attempt_count <= {MAX_TOTAL_ATTEMPTS}",
            name="repair_run_bounded_attempts",
        ),
        CheckConstraint(
            "same_provider_repairs_used >= 0 AND same_provider_repairs_used <= 2 "
            "AND alternate_provider_attempts_used >= 0 "
            "AND alternate_provider_attempts_used <= 1 "
            "AND fallback_renders_used >= 0 AND fallback_renders_used <= 1",
            name="repair_run_bounded_routes",
        ),
        CheckConstraint("total_repair_cost >= 0", name="repair_run_nonnegative_cost"),
        CheckConstraint(
            "final_qa_score IS NULL OR (final_qa_score >= 0 AND final_qa_score <= 100)",
            name="repair_run_score_range",
        ),
        CheckConstraint("advance_token > 0", name="repair_run_positive_advance_token"),
        # A review state always explains itself.
        CheckConstraint(
            "state <> 'HUMAN_REVIEW_REQUIRED' OR human_review_reason IS NOT NULL",
            name="repair_run_review_requires_reason",
        ),
        # Nothing locks without a selected attempt and the QA result that cleared it.
        CheckConstraint(
            "state <> 'LOCKED' OR (selected_attempt_id IS NOT NULL "
            "AND final_qa_result_id IS NOT NULL AND selected_asset_id IS NOT NULL)",
            name="repair_run_lock_requires_passing_selection",
        ),
        CheckConstraint(
            "selected_attempt_id IS NULL OR state = 'LOCKED'",
            name="repair_run_selection_implies_lock",
        ),
    )


class RepairAttemptRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One immutable historical generation or deterministic fallback render."""

    __tablename__ = "repair_attempts"
    repair_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("repair_runs.id", ondelete="CASCADE"), index=True
    )
    shot_id: Mapped[UUID] = mapped_column(ForeignKey("storyboard_shots.id", ondelete="CASCADE"))
    attempt_ordinal: Mapped[int] = mapped_column(Integer)
    attempt_kind: Mapped[str] = mapped_column(String(32))
    attempt_identity: Mapped[str] = mapped_column(String(64), unique=True)
    root_animation_attempt_id: Mapped[UUID] = mapped_column(
        ForeignKey("animation_generated_videos.id", ondelete="RESTRICT")
    )
    predecessor_attempt_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("repair_attempts.id", ondelete="RESTRICT")
    )
    provider_attempt_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("provider_attempts.id", ondelete="RESTRICT")
    )
    generated_video_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("animation_generated_videos.id", ondelete="RESTRICT")
    )
    provider: Mapped[str] = mapped_column(String(64), default="")
    model: Mapped[str] = mapped_column(String(128), default="")
    provider_operation_id: Mapped[str | None] = mapped_column(String(512))
    capability_profile_hash: Mapped[str | None] = mapped_column(String(64))
    prompt_hash: Mapped[str | None] = mapped_column(String(64))
    prompt_delta: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    seed: Mapped[int | None] = mapped_column(BigInteger)
    previous_seed: Mapped[int | None] = mapped_column(BigInteger)
    reference_asset_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    reference_asset_hashes: Mapped[list[str]] = mapped_column(JSON, default=list)
    output_asset_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    source_qa_result_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("visual_qa_results.id", ondelete="RESTRICT")
    )
    output_qa_result_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("visual_qa_results.id", ondelete="RESTRICT")
    )
    status: Mapped[str] = mapped_column(String(32))
    failure_category: Mapped[str | None] = mapped_column(String(32))
    failure_code: Mapped[str | None] = mapped_column(String(128))
    estimated_cost: Mapped[Decimal] = mapped_column(Money, default=0)
    actual_cost: Mapped[Decimal] = mapped_column(Money, default=0)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    reservation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("cost_reservations.id", ondelete="RESTRICT")
    )
    trace_context: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    #: The single authoritative attempt for the shot, set only after its own T20
    #: result passes. Enforced unique per shot by a partial index.
    selected: Mapped[bool] = mapped_column(Boolean, default=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("repair_run_id", "attempt_ordinal", name="uq_repair_attempt_ordinal"),
        Index("ix_repair_attempts_shot", "shot_id", "attempt_ordinal"),
        CheckConstraint(f"attempt_kind IN {ATTEMPT_KINDS}", name="repair_attempt_kind"),
        CheckConstraint(f"status IN {ATTEMPT_STATUSES}", name="repair_attempt_status"),
        CheckConstraint(
            f"failure_category IS NULL OR failure_category IN {FAILURE_CATEGORIES}",
            name="repair_attempt_failure_category",
        ),
        CheckConstraint(
            f"attempt_ordinal >= 0 AND attempt_ordinal <= {MAX_ATTEMPT_ORDINAL}",
            name="repair_attempt_bounded_ordinal",
        ),
        CheckConstraint("length(attempt_identity) = 64", name="repair_attempt_identity_length"),
        # The root attempt is the original generation and has no predecessor;
        # every repair attempt records exactly one.
        CheckConstraint(
            "(attempt_ordinal = 0) = (predecessor_attempt_id IS NULL)",
            name="repair_attempt_predecessor_presence",
        ),
        CheckConstraint(
            "predecessor_attempt_id IS NULL OR predecessor_attempt_id <> id",
            name="repair_attempt_no_self_reference",
        ),
        CheckConstraint(
            "estimated_cost >= 0 AND actual_cost >= 0", name="repair_attempt_nonnegative_cost"
        ),
        CheckConstraint("seed IS NULL OR seed >= 0", name="repair_attempt_nonnegative_seed"),
        # Selection is only ever earned by a fresh, passing T20 result.
        CheckConstraint(
            "NOT selected OR (output_qa_result_id IS NOT NULL AND status = 'passed')",
            name="repair_attempt_selection_requires_qa",
        ),
        CheckConstraint(
            "status <> 'passed' OR output_qa_result_id IS NOT NULL",
            name="repair_attempt_pass_requires_qa",
        ),
        # Exactly one authoritative attempt per shot, enforced by the database.
        Index(
            "uq_repair_attempt_selected",
            "shot_id",
            unique=True,
            sqlite_where=text("selected = 1"),
            postgresql_where=text("selected"),
        ),
    )


class RepairDecisionRecord(UUIDPrimaryKeyMixin, Base):
    """One recorded routing decision with the inputs it was based on."""

    __tablename__ = "repair_decisions"
    repair_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("repair_runs.id", ondelete="CASCADE"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer)
    source_attempt_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("repair_attempts.id", ondelete="SET NULL")
    )
    source_qa_result_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("visual_qa_results.id", ondelete="RESTRICT")
    )
    classification: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    failure_category: Mapped[str | None] = mapped_column(String(32))
    repair_codes: Mapped[list[str]] = mapped_column(JSON, default=list)
    route: Mapped[str] = mapped_column(String(48))
    rationale: Mapped[list[str]] = mapped_column(JSON, default=list)
    capability_profile_hash: Mapped[str | None] = mapped_column(String(64))
    budget_remaining: Mapped[Decimal | None] = mapped_column(Money)
    estimated_next_cost: Mapped[Decimal] = mapped_column(Money, default=0)
    human_review_reason: Mapped[str | None] = mapped_column(String(64))
    planner_version: Mapped[str] = mapped_column(String(64))
    policy_version: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        UniqueConstraint("repair_run_id", "sequence", name="uq_repair_decision_sequence"),
        CheckConstraint("sequence >= 0 AND sequence <= 16", name="repair_decision_sequence_range"),
        CheckConstraint(
            "estimated_next_cost >= 0 AND (budget_remaining IS NULL OR budget_remaining >= 0)",
            name="repair_decision_nonnegative_amounts",
        ),
        CheckConstraint(
            "route <> 'human_review_required' OR human_review_reason IS NOT NULL",
            name="repair_decision_review_requires_reason",
        ),
        CheckConstraint(
            f"failure_category IS NULL OR failure_category IN {FAILURE_CATEGORIES}",
            name="repair_decision_failure_category",
        ),
    )


class RepairFallbackRender(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One deterministic 2.5D parallax render and everything that produced it."""

    __tablename__ = "repair_fallback_renders"
    repair_attempt_id: Mapped[UUID] = mapped_column(
        ForeignKey("repair_attempts.id", ondelete="CASCADE"), unique=True
    )
    shot_id: Mapped[UUID] = mapped_column(ForeignKey("storyboard_shots.id", ondelete="CASCADE"))
    render_identity: Mapped[str] = mapped_column(String(64), unique=True)
    renderer_version: Mapped[str] = mapped_column(String(64))
    input_asset_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    input_asset_hashes: Mapped[list[str]] = mapped_column(JSON, default=list)
    render_parameters: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    exact_duration_us: Mapped[int] = mapped_column(BigInteger)
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    frame_rate: Mapped[str] = mapped_column(String(32))
    pixel_format: Mapped[str] = mapped_column(String(32))
    video_codec: Mapped[str] = mapped_column(String(32))
    ffmpeg_version: Mapped[str] = mapped_column(String(128))
    ffprobe_version: Mapped[str] = mapped_column(String(128))
    ffprobe_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    output_asset_id: Mapped[UUID] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    manifest_asset_id: Mapped[UUID] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    output_sha256: Mapped[str] = mapped_column(String(64))
    qa_result_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("visual_qa_results.id", ondelete="RESTRICT")
    )
    __table_args__ = (
        CheckConstraint(
            "exact_duration_us > 0 AND width > 0 AND height > 0",
            name="repair_fallback_positive_media",
        ),
        CheckConstraint("length(output_sha256) = 64", name="repair_fallback_hash_length"),
        CheckConstraint("length(render_identity) = 64", name="repair_fallback_identity_length"),
    )


class VeoOperationRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """The durable Veo long-running-operation checkpoint.

    The operation name is written immediately after submission and before the
    first poll, so an interrupted worker resumes the operation it already paid
    for instead of submitting a second one.
    """

    __tablename__ = "repair_veo_operations"
    repair_attempt_id: Mapped[UUID] = mapped_column(
        ForeignKey("repair_attempts.id", ondelete="CASCADE"), unique=True
    )
    provider_attempt_id: Mapped[UUID] = mapped_column(
        ForeignKey("provider_attempts.id", ondelete="RESTRICT"), unique=True
    )
    application_idempotency_key: Mapped[str] = mapped_column(String(255), unique=True)
    model: Mapped[str] = mapped_column(String(128))
    operation_name: Mapped[str | None] = mapped_column(String(512), unique=True)
    state: Mapped[str] = mapped_column(String(16))
    poll_count: Mapped[int] = mapped_column(Integer, default=0)
    submission_ambiguous: Mapped[bool] = mapped_column(Boolean, default=False)
    request_projection: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_code: Mapped[str | None] = mapped_column(String(128))
    failure_message: Mapped[str | None] = mapped_column(String(500))
    redacted_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    __table_args__ = (
        CheckConstraint(
            "state IN ('submitted','running','succeeded','failed','cancelled')",
            name="repair_veo_operation_state",
        ),
        CheckConstraint("poll_count >= 0", name="repair_veo_operation_poll_count"),
        # An ambiguous submission has no confirmed operation to poll, and a
        # running or succeeded one always does.
        CheckConstraint(
            "NOT submission_ambiguous OR operation_name IS NULL",
            name="repair_veo_operation_ambiguous_has_no_name",
        ),
        CheckConstraint(
            "state NOT IN ('running','succeeded') OR operation_name IS NOT NULL",
            name="repair_veo_operation_requires_name",
        ),
    )
