"""T10 episode analysis persistence."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_episode_analysis"
down_revision: str | None = "0005_workflow_evidence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "episode_analysis_runs",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("source_video_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_package_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("contract_version", sa.String(32), nullable=False),
        sa.Column("prompt_version", sa.String(32), nullable=False),
        sa.Column("provider_configuration_version", sa.String(64), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("status", sa.String(64), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("validation_report", sa.JSON()),
        sa.Column("error_code", sa.String(64)),
        sa.Column("selected", sa.Boolean(), nullable=False),
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "attempt_count >= 0", name=op.f("ck_episode_analysis_runs_analysis_attempt_nonnegative")
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_video_id"], ["source_videos.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["evidence_package_id"], ["evidence_packages.id"], ondelete="RESTRICT"
        ),
    )
    op.create_index(
        "uq_analysis_run_project_idempotency",
        "episode_analysis_runs",
        ["project_id", "idempotency_key"],
        unique=True,
    )
    op.create_table(
        "scene_analysis_checkpoints",
        sa.Column("analysis_run_id", sa.Uuid(), nullable=False),
        sa.Column("source_scene_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False, unique=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("provider_request_id", sa.String(255), unique=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("provider_result", sa.JSON()),
        sa.Column("validation_report", sa.JSON()),
        sa.Column("error_code", sa.String(64)),
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "sequence > 0", name=op.f("ck_scene_analysis_checkpoints_checkpoint_positive_sequence")
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name=op.f("ck_scene_analysis_checkpoints_checkpoint_attempt_nonnegative"),
        ),
        sa.ForeignKeyConstraint(
            ["analysis_run_id"], ["episode_analysis_runs.id"], ondelete="CASCADE"
        ),
    )
    op.create_index(
        "uq_checkpoint_run_scene",
        "scene_analysis_checkpoints",
        ["analysis_run_id", "source_scene_id"],
        unique=True,
    )
    op.create_table(
        "episode_analyses",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("analysis_run_id", sa.Uuid(), nullable=False, unique=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("canonical_analysis_asset_id", sa.Uuid(), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("character_count", sa.Integer(), nullable=False),
        sa.Column("location_count", sa.Integer(), nullable=False),
        sa.Column("scene_count", sa.Integer(), nullable=False),
        sa.Column("plot_beat_count", sa.Integer(), nullable=False),
        sa.Column("selected", sa.Boolean(), nullable=False),
        sa.Column("warnings", sa.JSON(), nullable=False),
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "version > 0", name=op.f("ck_episode_analyses_analysis_positive_version")
        ),
        sa.CheckConstraint(
            "duration_ms > 0", name=op.f("ck_episode_analyses_analysis_positive_duration")
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["analysis_run_id"], ["episode_analysis_runs.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["canonical_analysis_asset_id"], ["assets.id"], ondelete="RESTRICT"
        ),
    )
    op.create_index(
        "uq_episode_analysis_project_version",
        "episode_analyses",
        ["project_id", "version"],
        unique=True,
    )
    op.create_index(
        "uq_episode_analysis_selected",
        "episode_analyses",
        ["project_id"],
        unique=True,
        sqlite_where=sa.text("selected = 1"),
        postgresql_where=sa.text("selected"),
    )


def downgrade() -> None:
    op.drop_table("episode_analyses")
    op.drop_table("scene_analysis_checkpoints")
    op.drop_table("episode_analysis_runs")
