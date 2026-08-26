"""T11 compression and comedy script persistence.

``scripts`` and ``script_segments`` were reserved as generic placeholders by the T01
scaffolding migration. T11 is the first task to populate them, so this module defines
their full typed shape alongside the new run, plan, review, and edit tables rather than
creating a competing representation.
"""

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
    UniqueConstraint,
)
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Mapped, mapped_column

from vidgen.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ScriptGenerationRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "script_generation_runs"
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    episode_analysis_id: Mapped[UUID] = mapped_column(
        ForeignKey("episode_analyses.id", ondelete="RESTRICT")
    )
    idempotency_key: Mapped[str] = mapped_column(String(255))
    input_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(64))
    target_duration_ms: Mapped[int] = mapped_column(Integer)
    target_word_count: Mapped[int] = mapped_column(Integer)
    target_words_per_minute: Mapped[int] = mapped_column(Integer)
    humor_intensity: Mapped[float] = mapped_column(Float)
    recap_mode: Mapped[str] = mapped_column(String(32))
    provider_configuration_version: Mapped[str] = mapped_column(String(64))
    compressor_model: Mapped[str] = mapped_column(String(128))
    writer_model: Mapped[str] = mapped_column(String(128))
    editor_model: Mapped[str] = mapped_column(String(128))
    compressor_prompt_version: Mapped[str] = mapped_column(String(32))
    writer_prompt_version: Mapped[str] = mapped_column(String(32))
    editor_prompt_version: Mapped[str] = mapped_column(String(32))
    rubric_version: Mapped[str] = mapped_column(String(32))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    revision_count: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(64))
    __table_args__ = (
        Index("uq_script_run_project_idempotency", "project_id", "idempotency_key", unique=True),
        CheckConstraint("attempt_count >= 0", name="script_run_attempt_nonnegative"),
        CheckConstraint("revision_count >= 0", name="script_run_revision_nonnegative"),
        CheckConstraint("target_duration_ms > 0", name="script_run_positive_duration"),
        CheckConstraint("target_word_count > 0", name="script_run_positive_words"),
        CheckConstraint("target_words_per_minute > 0", name="script_run_positive_wpm"),
        CheckConstraint("humor_intensity BETWEEN 0 AND 1", name="script_run_humor_range"),
        CheckConstraint("length(input_hash) = 64", name="script_run_input_hash_length"),
    )


class CompressedPlotPlanRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "compressed_plot_plans"
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    generation_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("script_generation_runs.id", ondelete="CASCADE")
    )
    episode_analysis_id: Mapped[UUID] = mapped_column(
        ForeignKey("episode_analyses.id", ondelete="RESTRICT")
    )
    version: Mapped[int] = mapped_column(Integer)
    input_hash: Mapped[str] = mapped_column(String(64))
    canonical_plan_asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT")
    )
    selected_beat_count: Mapped[int] = mapped_column(Integer)
    omitted_beat_count: Mapped[int] = mapped_column(Integer)
    target_word_count: Mapped[int] = mapped_column(Integer)
    validation_report: Mapped[dict[str, Any]] = mapped_column(JSON)
    selected: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (
        Index("uq_plot_plan_run_version", "generation_run_id", "version", unique=True),
        Index(
            "uq_plot_plan_selected_run",
            "generation_run_id",
            unique=True,
            sqlite_where=sql_text("selected = 1"),
            postgresql_where=sql_text("selected"),
        ),
        CheckConstraint("version > 0", name="plot_plan_positive_version"),
        CheckConstraint("selected_beat_count > 0", name="plot_plan_positive_selected"),
        CheckConstraint("omitted_beat_count >= 0", name="plot_plan_nonnegative_omitted"),
        CheckConstraint("target_word_count > 0", name="plot_plan_positive_words"),
        CheckConstraint("length(input_hash) = 64", name="plot_plan_input_hash_length"),
    )


class Script(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "scripts"
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    generation_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("script_generation_runs.id", ondelete="RESTRICT")
    )
    episode_analysis_id: Mapped[UUID] = mapped_column(
        ForeignKey("episode_analyses.id", ondelete="RESTRICT")
    )
    compressed_plot_plan_id: Mapped[UUID] = mapped_column(
        ForeignKey("compressed_plot_plans.id", ondelete="RESTRICT")
    )
    parent_script_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("scripts.id", ondelete="SET NULL")
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(64), default="draft", nullable=False)
    target_word_count: Mapped[int] = mapped_column(Integer, nullable=False)
    actual_word_count: Mapped[int] = mapped_column(Integer, nullable=False)
    target_duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    humor_intensity: Mapped[float] = mapped_column(Float, nullable=False)
    canonical_script_asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT")
    )
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False)
    rubric_version: Mapped[str | None] = mapped_column(String(32))
    review_scores: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    selected: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    __table_args__ = (
        # Named to match the migration: T01 created this as "uq_scripts_project_id"
        # on (project_id, revision); T11 renames the column in place rather than
        # dropping and recreating the constraint (SQLite can't reliably reflect a
        # unique constraint created fresh inside a batch recreate by a later name).
        UniqueConstraint("project_id", "version", name="uq_scripts_project_id"),
        Index(
            "uq_scripts_selected_project",
            "project_id",
            unique=True,
            sqlite_where=sql_text("selected = 1"),
            postgresql_where=sql_text("selected"),
        ),
        CheckConstraint("version > 0", name="positive_version"),
        CheckConstraint("target_word_count > 0", name="positive_target_words"),
        CheckConstraint("actual_word_count >= 0", name="nonnegative_actual_words"),
        CheckConstraint("target_duration_ms > 0", name="positive_duration"),
        CheckConstraint("humor_intensity BETWEEN 0 AND 1", name="humor_range"),
    )


class ScriptSegment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "script_segments"
    script_id: Mapped[UUID] = mapped_column(
        ForeignKey("scripts.id", ondelete="CASCADE"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    # ``stable_segment_id`` is the cross-version content identity (the contract's
    # ``segment_id``): the same beat's segment keeps this value across revisions even
    # though each version persists its own row with its own primary key.
    stable_segment_id: Mapped[UUID] = mapped_column(nullable=False)
    segment_type: Mapped[str] = mapped_column(String(16), nullable=False)
    speaker_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    speaker_character_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("characters.id", ondelete="SET NULL")
    )
    anonymous_speaker_label: Mapped[str | None] = mapped_column(String(255))
    text: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    plot_beat_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    source_scene_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    joke_annotations: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    visual_gag: Mapped[str | None] = mapped_column(Text)
    estimated_duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    voice_direction: Mapped[str] = mapped_column(Text, default="", nullable=False)
    locked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    __table_args__ = (
        UniqueConstraint("script_id", "sequence", name="uq_script_segments_script_id"),
        Index("uq_script_segments_stable_id", "script_id", "stable_segment_id", unique=True),
        CheckConstraint("estimated_duration_ms > 0", name="positive_target_duration"),
        CheckConstraint("length(content_hash) = 64", name="hash_length"),
    )


class ScriptReview(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "script_reviews"
    script_id: Mapped[UUID] = mapped_column(ForeignKey("scripts.id", ondelete="CASCADE"))
    review_sequence: Mapped[int] = mapped_column(Integer)
    provider_request_id: Mapped[str | None] = mapped_column(String(255))
    attempt_number: Mapped[int] = mapped_column(Integer)
    rubric_version: Mapped[str] = mapped_column(String(32))
    scores: Mapped[dict[str, Any]] = mapped_column(JSON)
    issues: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    approval_recommendation: Mapped[str] = mapped_column(String(16))
    validation_report: Mapped[dict[str, Any]] = mapped_column(JSON)
    __table_args__ = (
        Index("uq_script_reviews_sequence", "script_id", "review_sequence", unique=True),
        Index(
            "uq_script_reviews_provider_request",
            "provider_request_id",
            unique=True,
            sqlite_where=sql_text("provider_request_id IS NOT NULL"),
            postgresql_where=sql_text("provider_request_id IS NOT NULL"),
        ),
        CheckConstraint("review_sequence > 0", name="script_review_positive_sequence"),
        CheckConstraint("attempt_number > 0", name="script_review_positive_attempt"),
    )


class ScriptEditRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "script_edits"
    review_id: Mapped[UUID] = mapped_column(ForeignKey("script_reviews.id", ondelete="CASCADE"))
    # The stable cross-version segment identity (see ``ScriptSegment.stable_segment_id``),
    # not a single row's primary key, since one edit can touch the same logical
    # segment across an old and a new version row.
    segment_id: Mapped[UUID] = mapped_column(nullable=False)
    old_content_hash: Mapped[str] = mapped_column(String(64))
    new_content_hash: Mapped[str] = mapped_column(String(64))
    old_text: Mapped[str] = mapped_column(Text)
    new_text: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    rubric_dimensions: Mapped[list[str]] = mapped_column(JSON, default=list)
    applied: Mapped[bool] = mapped_column(Boolean, default=True)
    __table_args__ = (
        CheckConstraint("length(old_content_hash) = 64", name="script_edit_old_hash_length"),
        CheckConstraint("length(new_content_hash) = 64", name="script_edit_new_hash_length"),
    )
