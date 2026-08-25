"""Add subtitle acquisition runs and subtitle-backed canonical transcripts.

Revision ID: 0004_subtitle_ingestion
Revises: 0003_transcription
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_subtitle_ingestion"
down_revision = "0003_transcription"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "subtitle_runs",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("source_video_id", sa.Uuid(), nullable=False),
        sa.Column("source_audio_asset_id", sa.Uuid(), nullable=True),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("status", sa.String(64), nullable=False),
        sa.Column("acquisition_mode", sa.String(32), nullable=False),
        sa.Column("provider", sa.String(128), nullable=True),
        sa.Column("parameters", sa.JSON(), nullable=False),
        sa.Column("selected_candidate_id", sa.String(255), nullable=True),
        sa.Column("quality_score", sa.Float(), nullable=True),
        sa.Column("coverage_score", sa.Float(), nullable=True),
        sa.Column("selected", sa.Boolean(), nullable=False),
        sa.Column("error_code", sa.String(128), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "quality_score IS NULL OR quality_score BETWEEN 0 AND 1",
            name=op.f("ck_subtitle_runs_quality_score_range"),
        ),
        sa.CheckConstraint(
            "coverage_score IS NULL OR coverage_score BETWEEN 0 AND 1",
            name=op.f("ck_subtitle_runs_coverage_score_range"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE", name=op.f("fk_sr_project")
        ),
        sa.ForeignKeyConstraint(
            ["source_video_id"],
            ["source_videos.id"],
            ondelete="CASCADE",
            name=op.f("fk_sr_source_video"),
        ),
        sa.ForeignKeyConstraint(
            ["source_audio_asset_id"],
            ["assets.id"],
            ondelete="RESTRICT",
            name=op.f("fk_sr_source_audio"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_subtitle_runs")),
        sa.UniqueConstraint("project_id", "idempotency_key", name="uq_sr_project_key"),
    )
    op.create_index("ix_subtitle_runs_project_id", "subtitle_runs", ["project_id"])
    op.create_index("ix_subtitle_runs_source_video_id", "subtitle_runs", ["source_video_id"])
    op.create_index(
        "ix_subtitle_runs_source_audio_asset_id", "subtitle_runs", ["source_audio_asset_id"]
    )
    op.create_index("ix_subtitle_runs_status", "subtitle_runs", ["status"])
    op.create_index(
        "uq_subtitle_runs_selected_project",
        "subtitle_runs",
        ["project_id"],
        unique=True,
        postgresql_where=sa.text("selected"),
        sqlite_where=sa.text("selected = 1"),
    )

    op.create_table(
        "subtitle_candidates",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("candidate_id", sa.String(255), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("provider", sa.String(128), nullable=False),
        sa.Column("provider_subtitle_id", sa.String(255), nullable=True),
        sa.Column("provider_file_id", sa.Integer(), nullable=True),
        sa.Column("asset_id", sa.Uuid(), nullable=True),
        sa.Column("stream_index", sa.Integer(), nullable=True),
        sa.Column("language", sa.String(32), nullable=True),
        sa.Column("subtitle_format", sa.String(32), nullable=False),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("selected", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("provider_request_id", sa.String(255), nullable=True),
        sa.Column("provider_metadata", sa.JSON(), nullable=False),
        sa.Column("quality", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(128), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "sequence >= 0", name=op.f("ck_subtitle_candidates_sequence_nonnegative")
        ),
        sa.CheckConstraint(
            "stream_index IS NULL OR stream_index >= 0",
            name=op.f("ck_subtitle_candidates_stream_nonnegative"),
        ),
        sa.CheckConstraint(
            "score IS NULL OR score BETWEEN 0 AND 1",
            name=op.f("ck_subtitle_candidates_score_range"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["subtitle_runs.id"], ondelete="CASCADE", name=op.f("fk_sc_run")
        ),
        sa.ForeignKeyConstraint(
            ["asset_id"], ["assets.id"], ondelete="RESTRICT", name=op.f("fk_sc_asset")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_subtitle_candidates")),
        sa.UniqueConstraint("run_id", "sequence", name="uq_sc_run_sequence"),
        sa.UniqueConstraint("run_id", "candidate_id", name="uq_sc_run_candidate"),
    )
    op.create_index("ix_subtitle_candidates_run_id", "subtitle_candidates", ["run_id"])
    op.create_index("ix_subtitle_candidates_asset_id", "subtitle_candidates", ["asset_id"])
    op.create_index(
        "uq_subtitle_candidates_selected_run",
        "subtitle_candidates",
        ["run_id"],
        unique=True,
        postgresql_where=sa.text("selected"),
        sqlite_where=sa.text("selected = 1"),
    )

    with op.batch_alter_table("transcripts") as batch:
        batch.alter_column("run_id", existing_type=sa.Uuid(), nullable=True)
        batch.add_column(sa.Column("subtitle_run_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            "fk_transcripts_subtitle_run_id_subtitle_runs",
            "subtitle_runs",
            ["subtitle_run_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch.create_unique_constraint("uq_transcripts_subtitle_run_id", ["subtitle_run_id"])
        batch.create_check_constraint(
            "exactly_one_origin_run",
            "(run_id IS NOT NULL AND subtitle_run_id IS NULL) OR "
            "(run_id IS NULL AND subtitle_run_id IS NOT NULL)",
        )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.execute(sa.text("SELECT 1 FROM subtitle_runs LIMIT 1")).first() is not None:
        raise RuntimeError(
            "cannot downgrade 0004_subtitle_ingestion while subtitle transcripts exist; "
            "export or delete subtitle runs before retrying"
        )
    with op.batch_alter_table("transcripts") as batch:
        batch.drop_constraint("exactly_one_origin_run", type_="check")
        batch.drop_constraint("uq_transcripts_subtitle_run_id", type_="unique")
        batch.drop_constraint("fk_transcripts_subtitle_run_id_subtitle_runs", type_="foreignkey")
        batch.drop_column("subtitle_run_id")
        batch.alter_column("run_id", existing_type=sa.Uuid(), nullable=False)
    op.drop_index("uq_subtitle_candidates_selected_run", table_name="subtitle_candidates")
    op.drop_index("ix_subtitle_candidates_asset_id", table_name="subtitle_candidates")
    op.drop_index("ix_subtitle_candidates_run_id", table_name="subtitle_candidates")
    op.drop_table("subtitle_candidates")
    op.drop_index("uq_subtitle_runs_selected_project", table_name="subtitle_runs")
    op.drop_index("ix_subtitle_runs_status", table_name="subtitle_runs")
    op.drop_index("ix_subtitle_runs_source_audio_asset_id", table_name="subtitle_runs")
    op.drop_index("ix_subtitle_runs_source_video_id", table_name="subtitle_runs")
    op.drop_index("ix_subtitle_runs_project_id", table_name="subtitle_runs")
    op.drop_table("subtitle_runs")
