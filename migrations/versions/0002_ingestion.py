"""Add project ownership, resumable uploads, and context-safe asset provenance.

Revision ID: 0002_ingestion
Revises: 0001_core
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_ingestion"
down_revision = "0001_core"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("projects") as batch:
        batch.add_column(
            sa.Column(
                "owner_subject",
                sa.String(length=255),
                nullable=False,
                server_default="local-user",
            )
        )

    with op.batch_alter_table("assets") as batch:
        batch.drop_constraint("uq_assets_sha256", type_="unique")
        batch.drop_constraint("uq_assets_storage_key", type_="unique")
        batch.alter_column(
            "byte_size", existing_type=sa.Integer(), type_=sa.BigInteger(), nullable=False
        )
        batch.create_index("ix_assets_sha256", ["sha256"], unique=False)
        batch.create_index(
            "uq_assets_project_idempotency",
            ["project_id", "idempotency_key"],
            unique=True,
            postgresql_where=sa.text("idempotency_key IS NOT NULL"),
            sqlite_where=sa.text("idempotency_key IS NOT NULL"),
        )

    op.create_table(
        "upload_sessions",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("owner_subject", sa.String(length=255), nullable=False),
        sa.Column("filename", sa.String(length=512), nullable=False),
        sa.Column("media_type", sa.String(length=255), nullable=False),
        sa.Column("expected_size", sa.BigInteger(), nullable=False),
        sa.Column("expected_sha256", sa.String(length=64), nullable=False),
        sa.Column("part_size", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("completed_asset_id", sa.Uuid(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "expected_size > 0", name=op.f("ck_upload_sessions_positive_expected_size")
        ),
        sa.CheckConstraint(
            "part_size > 0", name=op.f("ck_upload_sessions_positive_part_size")
        ),
        sa.CheckConstraint(
            "length(expected_sha256) = 64",
            name=op.f("ck_upload_sessions_expected_sha256_length"),
        ),
        sa.ForeignKeyConstraint(
            ["completed_asset_id"],
            ["assets.id"],
            name=op.f("fk_upload_sessions_completed_asset_id_assets"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_upload_sessions_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_upload_sessions")),
    )
    op.create_index("ix_upload_sessions_project_id", "upload_sessions", ["project_id"])
    op.create_index("ix_upload_sessions_status", "upload_sessions", ["status"])
    op.create_index(
        "ix_upload_sessions_project_status",
        "upload_sessions",
        ["project_id", "status"],
    )

    op.create_table(
        "upload_parts",
        sa.Column("upload_id", sa.Uuid(), nullable=False),
        sa.Column("part_number", sa.Integer(), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("storage_path", sa.String(length=1024), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "part_number >= 0", name=op.f("ck_upload_parts_nonnegative_part_number")
        ),
        sa.CheckConstraint("byte_size > 0", name=op.f("ck_upload_parts_positive_byte_size")),
        sa.CheckConstraint(
            "length(sha256) = 64", name=op.f("ck_upload_parts_part_sha256_length")
        ),
        sa.ForeignKeyConstraint(
            ["upload_id"],
            ["upload_sessions.id"],
            name=op.f("fk_upload_parts_upload_id_upload_sessions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_upload_parts")),
        sa.UniqueConstraint(
            "upload_id", "part_number", name=op.f("uq_upload_parts_upload_id")
        ),
    )
    op.create_index("ix_upload_parts_upload_id", "upload_parts", ["upload_id"])


def downgrade() -> None:
    connection = op.get_bind()
    duplicate = connection.execute(
        sa.text(
            "SELECT 1 FROM assets "
            "GROUP BY sha256 HAVING COUNT(*) > 1 "
            "UNION ALL "
            "SELECT 1 FROM assets "
            "GROUP BY storage_key HAVING COUNT(*) > 1 "
            "LIMIT 1"
        )
    ).first()
    if duplicate is not None:
        raise RuntimeError(
            "cannot downgrade 0002_ingestion while duplicate asset blobs exist; "
            "export or consolidate per-project provenance before retrying"
        )

    op.drop_index("ix_upload_parts_upload_id", table_name="upload_parts")
    op.drop_table("upload_parts")
    op.drop_index("ix_upload_sessions_project_status", table_name="upload_sessions")
    op.drop_index("ix_upload_sessions_status", table_name="upload_sessions")
    op.drop_index("ix_upload_sessions_project_id", table_name="upload_sessions")
    op.drop_table("upload_sessions")

    with op.batch_alter_table("assets") as batch:
        batch.drop_index("uq_assets_project_idempotency")
        batch.drop_index("ix_assets_sha256")
        batch.alter_column(
            "byte_size", existing_type=sa.BigInteger(), type_=sa.Integer(), nullable=False
        )
        batch.create_unique_constraint("uq_assets_storage_key", ["storage_key"])
        batch.create_unique_constraint("uq_assets_sha256", ["sha256"])

    with op.batch_alter_table("projects") as batch:
        batch.drop_column("owner_subject")
