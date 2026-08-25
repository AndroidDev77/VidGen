"""Add restartable transcription runs, chunks, transcripts, and speaker turns.

Revision ID: 0003_transcription
Revises: 0002_ingestion
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_transcription"
down_revision = "0002_ingestion"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "transcription_runs",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("source_video_id", sa.Uuid(), nullable=False),
        sa.Column("source_audio_asset_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("status", sa.String(64), nullable=False),
        sa.Column("language", sa.String(32), nullable=True),
        sa.Column("chunker_version", sa.String(32), nullable=False),
        sa.Column("provider", sa.String(128), nullable=False),
        sa.Column("transcription_model", sa.String(128), nullable=False),
        sa.Column("diarization_model", sa.String(128), nullable=False),
        sa.Column("parameters", sa.JSON(), nullable=False),
        sa.Column("coverage_score", sa.Float(), nullable=True),
        sa.Column("selected", sa.Boolean(), nullable=False),
        sa.Column("error_code", sa.String(128), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "coverage_score IS NULL OR coverage_score BETWEEN 0 AND 1",
            name=op.f("ck_transcription_runs_coverage_score_range"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE", name=op.f("fk_tr_project")
        ),
        sa.ForeignKeyConstraint(
            ["source_video_id"],
            ["source_videos.id"],
            ondelete="CASCADE",
            name=op.f("fk_tr_source_video"),
        ),
        sa.ForeignKeyConstraint(
            ["source_audio_asset_id"],
            ["assets.id"],
            ondelete="RESTRICT",
            name=op.f("fk_tr_source_audio"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_transcription_runs")),
        sa.UniqueConstraint(
            "project_id", "idempotency_key", name=op.f("uq_transcription_runs_project_id")
        ),
    )
    op.create_index("ix_transcription_runs_project_id", "transcription_runs", ["project_id"])
    op.create_index(
        "ix_transcription_runs_source_video_id", "transcription_runs", ["source_video_id"]
    )
    op.create_index(
        "ix_transcription_runs_source_audio_asset_id",
        "transcription_runs",
        ["source_audio_asset_id"],
    )
    op.create_index("ix_transcription_runs_status", "transcription_runs", ["status"])
    op.create_index(
        "uq_transcription_runs_selected_project",
        "transcription_runs",
        ["project_id"],
        unique=True,
        postgresql_where=sa.text("selected"),
        sqlite_where=sa.text("selected = 1"),
    )

    op.create_table(
        "transcription_chunks",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("chunk_asset_id", sa.Uuid(), nullable=False),
        sa.Column("source_start_seconds", sa.Float(), nullable=False),
        sa.Column("source_end_seconds", sa.Float(), nullable=False),
        sa.Column("overlap_before_seconds", sa.Float(), nullable=False),
        sa.Column("overlap_after_seconds", sa.Float(), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("provider_request_id", sa.String(255), nullable=True),
        sa.Column("diarization_request_id", sa.String(255), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("provider_result", sa.JSON(), nullable=False),
        sa.Column("diarization_result", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(128), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "sequence >= 0", name=op.f("ck_transcription_chunks_sequence_nonnegative")
        ),
        sa.CheckConstraint(
            "source_end_seconds > source_start_seconds",
            name=op.f("ck_transcription_chunks_source_interval_positive"),
        ),
        sa.CheckConstraint(
            "overlap_before_seconds >= 0",
            name=op.f("ck_transcription_chunks_overlap_before_nonnegative"),
        ),
        sa.CheckConstraint(
            "overlap_after_seconds >= 0",
            name=op.f("ck_transcription_chunks_overlap_after_nonnegative"),
        ),
        sa.CheckConstraint(
            "byte_size > 0", name=op.f("ck_transcription_chunks_byte_size_positive")
        ),
        sa.CheckConstraint(
            "length(sha256) = 64", name=op.f("ck_transcription_chunks_sha256_length")
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name=op.f("ck_transcription_chunks_attempt_count_nonnegative"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["transcription_runs.id"],
            ondelete="CASCADE",
            name=op.f("fk_tc_run"),
        ),
        sa.ForeignKeyConstraint(
            ["chunk_asset_id"],
            ["assets.id"],
            ondelete="RESTRICT",
            name=op.f("fk_tc_asset"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_transcription_chunks")),
        sa.UniqueConstraint("run_id", "sequence", name=op.f("uq_tc_run_sequence")),
        sa.UniqueConstraint("run_id", "chunk_asset_id", name=op.f("uq_tc_run_asset")),
        sa.UniqueConstraint("provider_request_id", name=op.f("uq_tc_provider_request")),
        sa.UniqueConstraint("diarization_request_id", name=op.f("uq_tc_diarization_request")),
    )
    op.create_index("ix_transcription_chunks_run_id", "transcription_chunks", ["run_id"])
    op.create_index("ix_transcription_chunks_status", "transcription_chunks", ["status"])

    op.create_table(
        "transcripts",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("language", sa.String(32), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("transcript_asset_id", sa.Uuid(), nullable=False),
        sa.Column("duration_seconds", sa.Float(), nullable=False),
        sa.Column("coverage_score", sa.Float(), nullable=False),
        sa.Column("selected", sa.Boolean(), nullable=False),
        sa.Column("warnings", sa.JSON(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("version > 0", name=op.f("ck_transcripts_version_positive")),
        sa.CheckConstraint("duration_seconds > 0", name=op.f("ck_transcripts_duration_positive")),
        sa.CheckConstraint(
            "coverage_score BETWEEN 0 AND 1", name=op.f("ck_transcripts_coverage_score_range")
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE", name=op.f("fk_t_project")
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["transcription_runs.id"],
            ondelete="CASCADE",
            name=op.f("fk_t_run"),
        ),
        sa.ForeignKeyConstraint(
            ["transcript_asset_id"],
            ["assets.id"],
            ondelete="RESTRICT",
            name=op.f("fk_t_asset"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_transcripts")),
        sa.UniqueConstraint("run_id", name=op.f("uq_transcripts_run_id")),
        sa.UniqueConstraint("transcript_asset_id", name=op.f("uq_transcripts_asset_id")),
        sa.UniqueConstraint("project_id", "version", name=op.f("uq_transcripts_project_id")),
    )
    op.create_index("ix_transcripts_project_id", "transcripts", ["project_id"])
    op.create_index(
        "uq_transcripts_selected_project",
        "transcripts",
        ["project_id"],
        unique=True,
        postgresql_where=sa.text("selected"),
        sqlite_where=sa.text("selected = 1"),
    )

    op.create_table(
        "transcript_segments",
        sa.Column("transcript_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("start_seconds", sa.Float(), nullable=False),
        sa.Column("end_seconds", sa.Float(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("speaker_label", sa.String(64), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("source_chunk_ids", sa.JSON(), nullable=False),
        sa.Column("words", sa.JSON(), nullable=False),
        sa.Column("provenance", sa.JSON(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "sequence >= 0", name=op.f("ck_transcript_segments_sequence_nonnegative")
        ),
        sa.CheckConstraint(
            "end_seconds > start_seconds",
            name=op.f("ck_transcript_segments_interval_positive"),
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR confidence BETWEEN 0 AND 1",
            name=op.f("ck_transcript_segments_confidence_range"),
        ),
        sa.ForeignKeyConstraint(
            ["transcript_id"],
            ["transcripts.id"],
            ondelete="CASCADE",
            name=op.f("fk_ts_transcript"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_transcript_segments")),
        sa.UniqueConstraint("transcript_id", "sequence", name=op.f("uq_ts_transcript_sequence")),
    )
    op.create_index(
        "ix_transcript_segments_transcript_id", "transcript_segments", ["transcript_id"]
    )

    op.create_table(
        "speaker_turns",
        sa.Column("transcript_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("speaker_label", sa.String(64), nullable=False),
        sa.Column("start_seconds", sa.Float(), nullable=False),
        sa.Column("end_seconds", sa.Float(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("source_chunk_ids", sa.JSON(), nullable=False),
        sa.Column("provider_metadata", sa.JSON(), nullable=False),
        sa.Column("alternate_mappings", sa.JSON(), nullable=False),
        sa.Column("warnings", sa.JSON(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("sequence >= 0", name=op.f("ck_speaker_turns_sequence_nonnegative")),
        sa.CheckConstraint(
            "end_seconds > start_seconds", name=op.f("ck_speaker_turns_interval_positive")
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR confidence BETWEEN 0 AND 1",
            name=op.f("ck_speaker_turns_confidence_range"),
        ),
        sa.ForeignKeyConstraint(
            ["transcript_id"],
            ["transcripts.id"],
            ondelete="CASCADE",
            name=op.f("fk_st_transcript"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_speaker_turns")),
        sa.UniqueConstraint("transcript_id", "sequence", name=op.f("uq_st_transcript_sequence")),
    )
    op.create_index("ix_speaker_turns_transcript_id", "speaker_turns", ["transcript_id"])


def downgrade() -> None:
    op.drop_index("ix_speaker_turns_transcript_id", table_name="speaker_turns")
    op.drop_table("speaker_turns")
    op.drop_index("ix_transcript_segments_transcript_id", table_name="transcript_segments")
    op.drop_table("transcript_segments")
    op.drop_index("uq_transcripts_selected_project", table_name="transcripts")
    op.drop_index("ix_transcripts_project_id", table_name="transcripts")
    op.drop_table("transcripts")
    op.drop_index("ix_transcription_chunks_status", table_name="transcription_chunks")
    op.drop_index("ix_transcription_chunks_run_id", table_name="transcription_chunks")
    op.drop_table("transcription_chunks")
    op.drop_index("uq_transcription_runs_selected_project", table_name="transcription_runs")
    op.drop_index("ix_transcription_runs_status", table_name="transcription_runs")
    op.drop_index("ix_transcription_runs_source_audio_asset_id", table_name="transcription_runs")
    op.drop_index("ix_transcription_runs_source_video_id", table_name="transcription_runs")
    op.drop_index("ix_transcription_runs_project_id", table_name="transcription_runs")
    op.drop_table("transcription_runs")
