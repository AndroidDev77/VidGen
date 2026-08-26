"""T13 storyboard generation and deterministic timing tables.

Revision ID: 0010_storyboard
Revises: 0009_narration
"""

from alembic import op
from sqlalchemy import inspect

from vidgen.db.storyboard_models import (
    StoryboardRepairAttempt,
    StoryboardRun,
    StoryboardSegmentCheckpoint,
    StoryboardShotRecord,
)

revision = "0010_storyboard"
down_revision = "0009_narration"
branch_labels = None
depends_on = None

_TABLES = (
    StoryboardRun.__table__,
    StoryboardSegmentCheckpoint.__table__,
    StoryboardShotRecord.__table__,
    StoryboardRepairAttempt.__table__,
)


def upgrade() -> None:
    bind = op.get_bind()
    for table in _TABLES:
        table.create(bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    names = set(inspect(bind).get_table_names())
    populated = [
        table.name
        for table in _TABLES
        if table.name in names and bind.execute(table.select().limit(1)).first() is not None
    ]
    if populated:
        raise RuntimeError(
            "unsafe T13 downgrade: storyboard provenance would be destroyed. Export or delete "
            "rows from " + ", ".join(populated) + " before downgrading."
        )
    for table in reversed(_TABLES):
        table.drop(bind, checkfirst=True)
