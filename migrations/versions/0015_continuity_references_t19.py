"""T19 immutable character and location continuity references.

Revision ID: 0015_continuity_references
Revises: 0014_review_ui
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_continuity_references"
down_revision: str | None = "0014_review_ui"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _identity_table(name: str, entity_table: str, entity_column: str) -> None:
    op.create_table(
        name,
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column(entity_column, sa.Uuid(), nullable=False),
        sa.Column("episode_analysis_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("identity", sa.JSON(), nullable=False),
        sa.Column("identity_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("approved_by", sa.String(255)),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint([entity_column], [f"{entity_table}.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["episode_analysis_id"], ["episode_analyses.id"]),
        sa.UniqueConstraint(entity_column, "version", name=f"uq_{name}_entity_version"),
        sa.UniqueConstraint("project_id", "identity_hash", name=f"uq_{name}_identity"),
        sa.CheckConstraint("version > 0", name=f"{name}_positive_version"),
        sa.CheckConstraint("length(identity_hash) = 64", name=f"{name}_hash_length"),
        sa.CheckConstraint(
            "status IN ('draft','approved','rejected','stale')", name=f"{name}_status"
        ),
        sa.CheckConstraint(
            "(status = 'approved' AND approved_by IS NOT NULL AND approved_at IS NOT NULL) "
            "OR (status <> 'approved' AND approved_at IS NULL)",
            name=f"{name}_approval_consistency",
        ),
    )


def _reference_table(name: str, identity_table: str) -> None:
    op.create_table(
        name,
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("identity_version_id", sa.Uuid(), nullable=False),
        sa.Column("reference_identity", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("provider_attempt_id", sa.Uuid()),
        sa.Column("manifest_asset_id", sa.Uuid()),
        sa.Column("primary_asset_id", sa.Uuid()),
        sa.Column("ordered_asset_ids", sa.JSON(), nullable=False),
        sa.Column("validation_report", sa.JSON(), nullable=False),
        sa.Column("approved_by", sa.String(255)),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["identity_version_id"], [f"{identity_table}.id"]),
        sa.ForeignKeyConstraint(["manifest_asset_id"], ["assets.id"]),
        sa.ForeignKeyConstraint(["primary_asset_id"], ["assets.id"]),
        sa.UniqueConstraint("reference_identity", name=f"uq_{name}_identity"),
        sa.CheckConstraint("length(reference_identity) = 64", name=f"{name}_hash_length"),
    )


def upgrade() -> None:
    _identity_table("character_identity_versions", "characters", "character_id")
    _identity_table("location_identity_versions", "locations", "location_id")
    _reference_table("character_reference_sets", "character_identity_versions")
    _reference_table("location_reference_sets", "location_identity_versions")
    op.create_table(
        "shot_reference_bindings",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("storyboard_id", sa.Uuid(), nullable=False),
        sa.Column("storyboard_shot_id", sa.Uuid(), nullable=False),
        sa.Column("bundle", sa.JSON(), nullable=False),
        sa.Column("bundle_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["storyboard_id"], ["storyboard_runs.id"]),
        sa.ForeignKeyConstraint(["storyboard_shot_id"], ["storyboard_shots.id"]),
        sa.UniqueConstraint("project_id", "bundle_hash", name="uq_shot_reference_bundle"),
        sa.CheckConstraint("length(bundle_hash) = 64", name="shot_reference_bundle_hash_length"),
    )


def downgrade() -> None:
    op.drop_table("shot_reference_bindings")
    op.drop_table("location_reference_sets")
    op.drop_table("character_reference_sets")
    op.drop_table("location_identity_versions")
    op.drop_table("character_identity_versions")
