"""SQLAlchemy metadata projection for T19 continuity tables."""

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Table,
    UniqueConstraint,
    Uuid,
)

from vidgen.db.base import Base


def identity_table(name: str, entity_table: str, entity_column: str) -> Table:
    return Table(
        name,
        Base.metadata,
        Column("project_id", Uuid(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        Column(
            entity_column,
            Uuid(),
            ForeignKey(f"{entity_table}.id", ondelete="CASCADE"),
            nullable=False,
        ),
        Column("episode_analysis_id", Uuid(), ForeignKey("episode_analyses.id"), nullable=False),
        Column("version", Integer(), nullable=False),
        Column("identity", JSON(), nullable=False),
        Column("identity_hash", String(64), nullable=False),
        Column("status", String(16), nullable=False),
        Column("approved_by", String(255)),
        Column("approved_at", DateTime(timezone=True)),
        Column("id", Uuid(), primary_key=True),
        Column("created_at", DateTime(timezone=True), nullable=False),
        Column("updated_at", DateTime(timezone=True), nullable=False),
        UniqueConstraint(entity_column, "version", name=f"uq_{name}_entity_version"),
        UniqueConstraint("project_id", "identity_hash", name=f"uq_{name}_identity"),
        CheckConstraint("version > 0", name=f"{name}_positive_version"),
        CheckConstraint("length(identity_hash) = 64", name=f"{name}_hash_length"),
        CheckConstraint("status IN ('draft','approved','rejected','stale')", name=f"{name}_status"),
        CheckConstraint(
            "(status = 'approved' AND approved_by IS NOT NULL "
            "AND approved_at IS NOT NULL) OR (status <> 'approved' "
            "AND approved_at IS NULL)",
            name=f"{name}_approval_consistency",
        ),
    )


character_identity_versions = identity_table(
    "character_identity_versions", "characters", "character_id"
)
location_identity_versions = identity_table(
    "location_identity_versions", "locations", "location_id"
)


def candidate_table(name: str, identity_table_name: str) -> Table:
    return Table(
        name,
        Base.metadata,
        Column(
            "identity_version_id", Uuid(), ForeignKey(f"{identity_table_name}.id"), nullable=False
        ),
        Column("source_asset_id", Uuid(), ForeignKey("assets.id"), nullable=False),
        Column("source_scene_id", Uuid(), nullable=False),
        Column("source_timestamp_ms", BigInteger(), nullable=False),
        Column("score", Float(), nullable=False),
        Column("score_components", JSON(), nullable=False),
        Column("state_classification", JSON(), nullable=False),
        Column("selected", Boolean(), nullable=False),
        Column("rejection_reason", String(255)),
        Column("id", Uuid(), primary_key=True),
        Column("created_at", DateTime(timezone=True), nullable=False),
        UniqueConstraint("identity_version_id", "source_asset_id", name=f"uq_{name}_source"),
        CheckConstraint("source_timestamp_ms >= 0", name=f"{name}_timestamp"),
        CheckConstraint("score >= 0 AND score <= 1", name=f"{name}_score"),
    )


character_reference_candidates = candidate_table(
    "character_reference_candidates", "character_identity_versions"
)
location_reference_candidates = candidate_table(
    "location_reference_candidates", "location_identity_versions"
)


def reference_table(name: str, identity_table_name: str) -> Table:
    return Table(
        name,
        Base.metadata,
        Column("project_id", Uuid(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        Column(
            "identity_version_id", Uuid(), ForeignKey(f"{identity_table_name}.id"), nullable=False
        ),
        Column("reference_identity", String(64), nullable=False),
        Column("status", String(16), nullable=False),
        Column("provider_attempt_id", Uuid()),
        Column("manifest_asset_id", Uuid(), ForeignKey("assets.id")),
        Column("primary_asset_id", Uuid(), ForeignKey("assets.id")),
        Column("ordered_asset_ids", JSON(), nullable=False),
        Column("validation_report", JSON(), nullable=False),
        Column("row_version", Integer(), nullable=False, server_default="1"),
        Column("approved_by", String(255)),
        Column("approved_at", DateTime(timezone=True)),
        Column("id", Uuid(), primary_key=True),
        Column("created_at", DateTime(timezone=True), nullable=False),
        Column("updated_at", DateTime(timezone=True), nullable=False),
        UniqueConstraint("reference_identity", name=f"uq_{name}_identity"),
        CheckConstraint("length(reference_identity) = 64", name=f"{name}_hash_length"),
        CheckConstraint("row_version > 0", name=f"{name}_positive_row_version"),
    )


character_reference_sets = reference_table(
    "character_reference_sets", "character_identity_versions"
)
location_reference_sets = reference_table("location_reference_sets", "location_identity_versions")


def snapshot_table(name: str, identity_table_name: str) -> Table:
    return Table(
        name,
        Base.metadata,
        Column(
            "identity_version_id", Uuid(), ForeignKey(f"{identity_table_name}.id"), nullable=False
        ),
        Column("storyboard_shot_id", Uuid(), ForeignKey("storyboard_shots.id"), nullable=False),
        Column("chronology_interval", JSON(), nullable=False),
        Column("state", JSON(), nullable=False),
        Column("evidence_references", JSON(), nullable=False),
        Column("snapshot_hash", String(64), nullable=False),
        Column("resolver_version", String(64), nullable=False),
        Column("id", Uuid(), primary_key=True),
        Column("created_at", DateTime(timezone=True), nullable=False),
        UniqueConstraint(
            "identity_version_id", "storyboard_shot_id", "snapshot_hash", name=f"uq_{name}_identity"
        ),
        CheckConstraint("length(snapshot_hash) = 64", name=f"{name}_hash_length"),
    )


character_state_snapshots = snapshot_table(
    "character_state_snapshots", "character_identity_versions"
)
location_state_snapshots = snapshot_table("location_state_snapshots", "location_identity_versions")
reference_approvals = Table(
    "reference_approvals",
    Base.metadata,
    Column("project_id", Uuid(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
    Column("reference_set_id", Uuid(), nullable=False),
    Column("reference_kind", String(16), nullable=False),
    Column("identity_version_id", Uuid(), nullable=False),
    Column("approved_by", String(255), nullable=False),
    Column("upstream_lineage_hash", String(64), nullable=False),
    Column("approved_at", DateTime(timezone=True), nullable=False),
    Column("idempotency_key", String(255), nullable=False),
    Column("id", Uuid(), primary_key=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint(
        "project_id",
        "reference_kind",
        "reference_set_id",
        "idempotency_key",
        name="uq_reference_approval_idempotency",
    ),
    CheckConstraint("reference_kind IN ('character','location')", name="reference_approval_kind"),
    CheckConstraint("length(upstream_lineage_hash) = 64", name="reference_approval_lineage_hash"),
)
shot_reference_bindings = Table(
    "shot_reference_bindings",
    Base.metadata,
    Column("project_id", Uuid(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
    Column("storyboard_id", Uuid(), ForeignKey("storyboard_runs.id"), nullable=False),
    Column("storyboard_shot_id", Uuid(), ForeignKey("storyboard_shots.id"), nullable=False),
    Column("bundle", JSON(), nullable=False),
    Column("bundle_hash", String(64), nullable=False),
    Column("status", String(16), nullable=False),
    Column("id", Uuid(), primary_key=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("project_id", "bundle_hash", name="uq_shot_reference_bundle"),
    CheckConstraint("length(bundle_hash) = 64", name="shot_reference_bundle_hash_length"),
)
