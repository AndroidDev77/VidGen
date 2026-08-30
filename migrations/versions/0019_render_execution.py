"""T17b durable render execution: claims, leases, checkpoints and output identity

T17 delivered the deterministic rendering library but nothing executed a queued
render job. T17b adds the execution state that makes a render job safe to run,
resume and reclaim across processes: who holds it, until when, how far it got,
what it produced, and why it failed.

The migration is additive and preserves every existing render job. Existing rows
get an unclaimed lease, a zero attempt count and zero progress, which is exactly
the state a queued job should have.

Revision ID: 0019_render_execution
Revises: 0018_final_editorial_qa
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019_render_execution"
down_revision: str | None = "0018_final_editorial_qa"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = (
    "claimed_by",
    "claimed_at",
    "lease_expires_at",
    "heartbeat_at",
    "attempt_count",
    "progress_percent",
    "checkpoint",
    "cancel_requested",
    "failure_classification",
    "input_selection",
    "output_sha256",
    "renderer_version",
    "trace_id",
)

_CONSTRAINTS = (
    ("render_output_hash_length", "output_sha256 IS NULL OR length(output_sha256) = 64"),
    ("render_progress_percent_range", "progress_percent BETWEEN 0 AND 100"),
    ("render_attempt_count_nonnegative", "attempt_count >= 0"),
    (
        "render_complete_has_measurements",
        "status <> 'render_complete' OR (output_sha256 IS NOT NULL AND "
        "measured_duration_us IS NOT NULL AND completed_at IS NOT NULL)",
    ),
)


def upgrade() -> None:
    with op.batch_alter_table("render_jobs", schema=None) as batch:
        batch.add_column(sa.Column("claimed_by", sa.String(length=128), nullable=True))
        batch.add_column(sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(
            sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column("progress_percent", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(sa.Column("checkpoint", sa.String(length=64), nullable=True))
        batch.add_column(
            sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch.add_column(sa.Column("failure_classification", sa.String(length=32), nullable=True))
        batch.add_column(
            sa.Column("input_selection", sa.JSON(), nullable=False, server_default="{}")
        )
        batch.add_column(sa.Column("output_sha256", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("renderer_version", sa.String(length=32), nullable=True))
        batch.add_column(sa.Column("trace_id", sa.String(length=64), nullable=True))
        for name, expression in _CONSTRAINTS:
            batch.create_check_constraint(name, sa.text(expression))
        batch.create_index("ix_render_jobs_lease", ["status", "lease_expires_at"], unique=False)


def downgrade() -> None:
    # A completed render keeps every T17 output column it already had, so the
    # downgrade only removes the execution bookkeeping T17b introduced.
    with op.batch_alter_table("render_jobs", schema=None) as batch:
        batch.drop_index("ix_render_jobs_lease")
        for name, _expression in _CONSTRAINTS:
            batch.drop_constraint(name, type_="check")
        for column in _COLUMNS:
            batch.drop_column(column)
