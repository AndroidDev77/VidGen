"""Make every Comedy Editor pass a reviewable checkpoint.

Three additive columns. ``scripts.editing_pass`` records which editing pass
produced a version (0 for the writer's draft), and ``scripts.rejection_reason``
keeps the reviewer's feedback with the version it rejected, where the next pass
reads it from. ``script_generation_runs.max_editing_passes`` records the pass
budget a run was opened with, so a deployment that changes the default never
silently rewrites the budget of a run already waiting on its reviewer.

Existing rows keep their meaning: a version written before passes were counted
reads as pass 0 with no rejection, and an existing run gets the historical
budget of three passes.

The downgrade drops the three columns. A rejection reason recorded on a version
is lost with it, which is the only lineage this migration owns.

Revision ID: 0024_script_editing_passes
Revises: 0023_visual_qa_force_approval
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024_script_editing_passes"
down_revision: str | None = "0023_visual_qa_force_approval"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("scripts", schema=None) as batch:
        batch.add_column(
            sa.Column("editing_pass", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(sa.Column("rejection_reason", sa.Text(), nullable=True))
        batch.create_check_constraint(
            "script_editing_pass_nonnegative", sa.text("editing_pass >= 0")
        )
    with op.batch_alter_table("script_generation_runs", schema=None) as batch:
        batch.add_column(
            sa.Column("max_editing_passes", sa.Integer(), nullable=False, server_default="3")
        )
        batch.create_check_constraint(
            "script_run_positive_max_passes", sa.text("max_editing_passes > 0")
        )


def downgrade() -> None:
    with op.batch_alter_table("script_generation_runs", schema=None) as batch:
        batch.drop_constraint("script_run_positive_max_passes", type_="check")
        batch.drop_column("max_editing_passes")
    with op.batch_alter_table("scripts", schema=None) as batch:
        batch.drop_constraint("script_editing_pass_nonnegative", type_="check")
        batch.drop_column("rejection_reason")
        batch.drop_column("editing_pass")
