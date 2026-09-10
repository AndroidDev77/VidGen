"""Persist the routing decision behind every animation item.

Purely additive: one nullable JSON column on ``animation_items`` carrying the
bounded ``RoutingDecision`` - routing-policy version, quality mode, hero
designation, reason code and estimate - that selected the item's model. Every
existing item keeps its rows untouched with a null decision, which is exactly
what "routed before decisions were persisted" means.

The downgrade drops the column. Nothing else references it, so no lineage is
lost beyond the recorded reason itself.

Revision ID: 0022_generation_routing
Revises: 0021_control_plane
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022_generation_routing"
down_revision: str | None = "0021_control_plane"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("animation_items", schema=None) as batch:
        batch.add_column(sa.Column("routing_decision", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("animation_items", schema=None) as batch:
        batch.drop_column("routing_decision")
