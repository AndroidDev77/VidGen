"""Create the VidGen core relational model.

Revision ID: 0001_core
Revises: None
"""

from __future__ import annotations

from alembic import op

from vidgen.db.base import Base
import vidgen.db.models  # noqa: F401

revision = "0001_core"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind(), checkfirst=False)


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind(), checkfirst=False)

