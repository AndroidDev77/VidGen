"""T11 compression and comedy script pipeline persistence.

Revision ID: 0007_script_generation
Revises: 0006_episode_analysis
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_script_generation"
down_revision: str | None = "0006_episode_analysis"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "script_generation_runs",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("episode_analysis_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(64), nullable=False),
        sa.Column("target_duration_ms", sa.Integer(), nullable=False),
        sa.Column("target_word_count", sa.Integer(), nullable=False),
        sa.Column("target_words_per_minute", sa.Integer(), nullable=False),
        sa.Column("humor_intensity", sa.Float(), nullable=False),
        sa.Column("recap_mode", sa.String(32), nullable=False),
        sa.Column("provider_configuration_version", sa.String(64), nullable=False),
        sa.Column("compressor_model", sa.String(128), nullable=False),
        sa.Column("writer_model", sa.String(128), nullable=False),
        sa.Column("editor_model", sa.String(128), nullable=False),
        sa.Column("compressor_prompt_version", sa.String(32), nullable=False),
        sa.Column("writer_prompt_version", sa.String(32), nullable=False),
        sa.Column("editor_prompt_version", sa.String(32), nullable=False),
        sa.Column("rubric_version", sa.String(32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("revision_count", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "attempt_count >= 0", name=op.f("ck_script_generation_runs_script_run_attempt_nonnegative")
        ),
        sa.CheckConstraint(
            "revision_count >= 0",
            name=op.f("ck_script_generation_runs_script_run_revision_nonnegative"),
        ),
        sa.CheckConstraint(
            "target_duration_ms > 0", name=op.f("ck_script_generation_runs_script_run_positive_duration")
        ),
        sa.CheckConstraint(
            "target_word_count > 0", name=op.f("ck_script_generation_runs_script_run_positive_words")
        ),
        sa.CheckConstraint(
            "target_words_per_minute > 0",
            name=op.f("ck_script_generation_runs_script_run_positive_wpm"),
        ),
        sa.CheckConstraint(
            "humor_intensity BETWEEN 0 AND 1",
            name=op.f("ck_script_generation_runs_script_run_humor_range"),
        ),
        sa.CheckConstraint(
            "length(input_hash) = 64",
            name=op.f("ck_script_generation_runs_script_run_input_hash_length"),
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["episode_analysis_id"], ["episode_analyses.id"], ondelete="RESTRICT"
        ),
    )
    op.create_index(
        "uq_script_run_project_idempotency",
        "script_generation_runs",
        ["project_id", "idempotency_key"],
        unique=True,
    )

    op.create_table(
        "compressed_plot_plans",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("generation_run_id", sa.Uuid(), nullable=False),
        sa.Column("episode_analysis_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("canonical_plan_asset_id", sa.Uuid(), nullable=False),
        sa.Column("selected_beat_count", sa.Integer(), nullable=False),
        sa.Column("omitted_beat_count", sa.Integer(), nullable=False),
        sa.Column("target_word_count", sa.Integer(), nullable=False),
        sa.Column("validation_report", sa.JSON(), nullable=False),
        sa.Column("selected", sa.Boolean(), nullable=False),
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("version > 0", name=op.f("ck_compressed_plot_plans_plot_plan_positive_version")),
        sa.CheckConstraint(
            "selected_beat_count > 0", name=op.f("ck_compressed_plot_plans_plot_plan_positive_selected")
        ),
        sa.CheckConstraint(
            "omitted_beat_count >= 0", name=op.f("ck_compressed_plot_plans_plot_plan_nonnegative_omitted")
        ),
        sa.CheckConstraint(
            "target_word_count > 0", name=op.f("ck_compressed_plot_plans_plot_plan_positive_words")
        ),
        sa.CheckConstraint(
            "length(input_hash) = 64", name=op.f("ck_compressed_plot_plans_plot_plan_input_hash_length")
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["generation_run_id"], ["script_generation_runs.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["episode_analysis_id"], ["episode_analyses.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["canonical_plan_asset_id"], ["assets.id"], ondelete="RESTRICT"),
    )
    op.create_index(
        "uq_plot_plan_run_version",
        "compressed_plot_plans",
        ["generation_run_id", "version"],
        unique=True,
    )
    op.create_index(
        "uq_plot_plan_selected_run",
        "compressed_plot_plans",
        ["generation_run_id"],
        unique=True,
        sqlite_where=sa.text("selected = 1"),
        postgresql_where=sa.text("selected"),
    )

    with op.batch_alter_table("scripts") as batch:
        # Renaming the column carries the existing "uq_scripts_project_id" unique
        # constraint's column reference along with it (verified: SQLite preserves the
        # named constraint through a batch recreate rename), so it becomes the
        # (project_id, version) uniqueness guarantee without being dropped and
        # recreated — a constraint created fresh via batch mode on SQLite loses its
        # name across a later reflect/drop cycle, so recreating it here is avoided.
        batch.drop_column("contract")
        batch.alter_column("revision", new_column_name="version", existing_type=sa.Integer())
        batch.add_column(sa.Column("generation_run_id", sa.Uuid(), nullable=False))
        batch.add_column(sa.Column("episode_analysis_id", sa.Uuid(), nullable=False))
        batch.add_column(sa.Column("compressed_plot_plan_id", sa.Uuid(), nullable=False))
        batch.add_column(sa.Column("parent_script_id", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("target_word_count", sa.Integer(), nullable=False))
        batch.add_column(sa.Column("actual_word_count", sa.Integer(), nullable=False))
        batch.add_column(sa.Column("target_duration_ms", sa.Integer(), nullable=False))
        batch.add_column(sa.Column("humor_intensity", sa.Float(), nullable=False))
        batch.add_column(sa.Column("canonical_script_asset_id", sa.Uuid(), nullable=False))
        batch.add_column(sa.Column("prompt_version", sa.String(32), nullable=False))
        batch.add_column(sa.Column("rubric_version", sa.String(32), nullable=True))
        batch.add_column(sa.Column("review_scores", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("selected", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.create_check_constraint(op.f("ck_scripts_positive_version"), "version > 0")
        batch.create_check_constraint(
            op.f("ck_scripts_positive_target_words"), "target_word_count > 0"
        )
        batch.create_check_constraint(
            op.f("ck_scripts_nonnegative_actual_words"), "actual_word_count >= 0"
        )
        batch.create_check_constraint(op.f("ck_scripts_positive_duration"), "target_duration_ms > 0")
        batch.create_check_constraint(op.f("ck_scripts_humor_range"), "humor_intensity BETWEEN 0 AND 1")
        batch.create_foreign_key(
            op.f("fk_scripts_generation_run_id_script_generation_runs"),
            "script_generation_runs",
            ["generation_run_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_foreign_key(
            op.f("fk_scripts_episode_analysis_id_episode_analyses"),
            "episode_analyses",
            ["episode_analysis_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_foreign_key(
            op.f("fk_scripts_compressed_plot_plan_id_compressed_plot_plans"),
            "compressed_plot_plans",
            ["compressed_plot_plan_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_foreign_key(
            op.f("fk_scripts_parent_script_id_scripts"),
            "scripts",
            ["parent_script_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_foreign_key(
            op.f("fk_scripts_canonical_script_asset_id_assets"),
            "assets",
            ["canonical_script_asset_id"],
            ["id"],
            ondelete="RESTRICT",
        )
    op.create_index(
        "uq_scripts_selected_project",
        "scripts",
        ["project_id"],
        unique=True,
        sqlite_where=sa.text("selected = 1"),
        postgresql_where=sa.text("selected"),
    )

    with op.batch_alter_table("script_segments") as batch:
        batch.drop_constraint(op.f("ck_script_segments_positive_target_duration"), type_="check")
        batch.drop_column("narration")
        batch.drop_column("target_duration_seconds")
        batch.drop_column("measured_duration_seconds")
        batch.drop_column("contract")
        batch.add_column(sa.Column("stable_segment_id", sa.Uuid(), nullable=False))
        batch.add_column(sa.Column("segment_type", sa.String(16), nullable=False))
        batch.add_column(sa.Column("speaker_kind", sa.String(16), nullable=False))
        batch.add_column(sa.Column("speaker_character_id", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("anonymous_speaker_label", sa.String(255), nullable=True))
        batch.add_column(sa.Column("text", sa.Text(), nullable=False))
        batch.add_column(sa.Column("content_hash", sa.String(64), nullable=False))
        batch.add_column(sa.Column("plot_beat_ids", sa.JSON(), nullable=False))
        batch.add_column(sa.Column("source_scene_ids", sa.JSON(), nullable=False))
        batch.add_column(sa.Column("joke_annotations", sa.JSON(), nullable=False))
        batch.add_column(sa.Column("visual_gag", sa.Text(), nullable=True))
        batch.add_column(sa.Column("estimated_duration_ms", sa.Integer(), nullable=False))
        batch.add_column(
            sa.Column("voice_direction", sa.Text(), nullable=False, server_default="")
        )
        batch.add_column(
            sa.Column("locked", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch.create_check_constraint(
            op.f("ck_script_segments_positive_target_duration"), "estimated_duration_ms > 0"
        )
        batch.create_check_constraint(
            op.f("ck_script_segments_hash_length"), "length(content_hash) = 64"
        )
        batch.create_foreign_key(
            op.f("fk_script_segments_speaker_character_id_characters"),
            "characters",
            ["speaker_character_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.create_index(
        "uq_script_segments_stable_id",
        "script_segments",
        ["script_id", "stable_segment_id"],
        unique=True,
    )
    op.create_table(
        "script_reviews",
        sa.Column("script_id", sa.Uuid(), nullable=False),
        sa.Column("review_sequence", sa.Integer(), nullable=False),
        sa.Column("provider_request_id", sa.String(255)),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("rubric_version", sa.String(32), nullable=False),
        sa.Column("scores", sa.JSON(), nullable=False),
        sa.Column("issues", sa.JSON(), nullable=False),
        sa.Column("approval_recommendation", sa.String(16), nullable=False),
        sa.Column("validation_report", sa.JSON(), nullable=False),
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "review_sequence > 0", name=op.f("ck_script_reviews_script_review_positive_sequence")
        ),
        sa.CheckConstraint(
            "attempt_number > 0", name=op.f("ck_script_reviews_script_review_positive_attempt")
        ),
        sa.ForeignKeyConstraint(["script_id"], ["scripts.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "uq_script_reviews_sequence", "script_reviews", ["script_id", "review_sequence"], unique=True
    )
    op.create_index(
        "uq_script_reviews_provider_request",
        "script_reviews",
        ["provider_request_id"],
        unique=True,
        sqlite_where=sa.text("provider_request_id IS NOT NULL"),
        postgresql_where=sa.text("provider_request_id IS NOT NULL"),
    )

    op.create_table(
        "script_edits",
        sa.Column("review_id", sa.Uuid(), nullable=False),
        sa.Column("segment_id", sa.Uuid(), nullable=False),
        sa.Column("old_content_hash", sa.String(64), nullable=False),
        sa.Column("new_content_hash", sa.String(64), nullable=False),
        sa.Column("old_text", sa.Text(), nullable=False),
        sa.Column("new_text", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("rubric_dimensions", sa.JSON(), nullable=False),
        sa.Column("applied", sa.Boolean(), nullable=False),
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(old_content_hash) = 64", name=op.f("ck_script_edits_script_edit_old_hash_length")
        ),
        sa.CheckConstraint(
            "length(new_content_hash) = 64", name=op.f("ck_script_edits_script_edit_new_hash_length")
        ),
        sa.ForeignKeyConstraint(["review_id"], ["script_reviews.id"], ondelete="CASCADE"),
    )


def downgrade() -> None:
    op.drop_table("script_edits")
    op.drop_index("uq_script_reviews_provider_request", table_name="script_reviews")
    op.drop_index("uq_script_reviews_sequence", table_name="script_reviews")
    op.drop_table("script_reviews")

    op.drop_index("uq_script_segments_stable_id", table_name="script_segments")
    with op.batch_alter_table("script_segments") as batch:
        batch.drop_constraint(
            op.f("fk_script_segments_speaker_character_id_characters"), type_="foreignkey"
        )
        batch.drop_constraint(op.f("ck_script_segments_hash_length"), type_="check")
        batch.drop_constraint(op.f("ck_script_segments_positive_target_duration"), type_="check")
        batch.drop_column("locked")
        batch.drop_column("voice_direction")
        batch.drop_column("estimated_duration_ms")
        batch.drop_column("visual_gag")
        batch.drop_column("joke_annotations")
        batch.drop_column("source_scene_ids")
        batch.drop_column("plot_beat_ids")
        batch.drop_column("content_hash")
        batch.drop_column("text")
        batch.drop_column("anonymous_speaker_label")
        batch.drop_column("speaker_character_id")
        batch.drop_column("speaker_kind")
        batch.drop_column("segment_type")
        batch.drop_column("stable_segment_id")
        batch.add_column(sa.Column("narration", sa.Text(), nullable=True))
        batch.add_column(sa.Column("target_duration_seconds", sa.Float(), nullable=True))
        batch.add_column(sa.Column("measured_duration_seconds", sa.Float(), nullable=True))
        batch.add_column(sa.Column("contract", sa.JSON(), nullable=True))
        batch.create_check_constraint(
            op.f("ck_script_segments_positive_target_duration"),
            "target_duration_seconds > 0 OR target_duration_seconds IS NULL",
        )

    op.drop_index("uq_scripts_selected_project", table_name="scripts")
    with op.batch_alter_table("scripts") as batch:
        batch.drop_constraint(
            op.f("fk_scripts_canonical_script_asset_id_assets"), type_="foreignkey"
        )
        batch.drop_constraint(op.f("fk_scripts_parent_script_id_scripts"), type_="foreignkey")
        batch.drop_constraint(
            op.f("fk_scripts_compressed_plot_plan_id_compressed_plot_plans"), type_="foreignkey"
        )
        batch.drop_constraint(
            op.f("fk_scripts_episode_analysis_id_episode_analyses"), type_="foreignkey"
        )
        batch.drop_constraint(
            op.f("fk_scripts_generation_run_id_script_generation_runs"), type_="foreignkey"
        )
        batch.drop_constraint(op.f("ck_scripts_humor_range"), type_="check")
        batch.drop_constraint(op.f("ck_scripts_positive_duration"), type_="check")
        batch.drop_constraint(op.f("ck_scripts_nonnegative_actual_words"), type_="check")
        batch.drop_constraint(op.f("ck_scripts_positive_target_words"), type_="check")
        batch.drop_constraint(op.f("ck_scripts_positive_version"), type_="check")
        batch.drop_column("selected")
        batch.drop_column("review_scores")
        batch.drop_column("rubric_version")
        batch.drop_column("prompt_version")
        batch.drop_column("canonical_script_asset_id")
        batch.drop_column("humor_intensity")
        batch.drop_column("target_duration_ms")
        batch.drop_column("actual_word_count")
        batch.drop_column("target_word_count")
        batch.drop_column("parent_script_id")
        batch.drop_column("compressed_plot_plan_id")
        batch.drop_column("episode_analysis_id")
        batch.drop_column("generation_run_id")
        # The rename below carries "uq_scripts_project_id" back to (project_id,
        # revision) automatically; see the matching note in upgrade().
        batch.alter_column("version", new_column_name="revision", existing_type=sa.Integer())
        batch.add_column(sa.Column("contract", sa.JSON(), nullable=True))

    op.drop_index("uq_plot_plan_selected_run", table_name="compressed_plot_plans")
    op.drop_index("uq_plot_plan_run_version", table_name="compressed_plot_plans")
    op.drop_table("compressed_plot_plans")

    op.drop_index("uq_script_run_project_idempotency", table_name="script_generation_runs")
    op.drop_table("script_generation_runs")
