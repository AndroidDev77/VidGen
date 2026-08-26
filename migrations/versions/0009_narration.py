"""T12 narration tables.

Revision ID: 0009_narration
Revises: 0008_script_generation
"""

from alembic import op
from sqlalchemy import inspect

from vidgen.db.narration_models import (
    NarrationAttemptRecord,
    NarrationRun,
    NarrationSegment,
    VoiceProfileRecord,
)

revision = "0009_narration"
down_revision = "0008_script_generation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    for table in (
        VoiceProfileRecord.__table__,
        NarrationRun.__table__,
        NarrationSegment.__table__,
        NarrationAttemptRecord.__table__,
    ):
        table.create(bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    names = set(inspect(bind).get_table_names())
    if (
        "narration_runs" in names
        and bind.execute(NarrationRun.__table__.select().limit(1)).first() is not None
    ):
        raise RuntimeError("unsafe T12 downgrade: delete or export narration runs and assets first")
    for table in (
        NarrationAttemptRecord.__table__,
        NarrationSegment.__table__,
        NarrationRun.__table__,
        VoiceProfileRecord.__table__,
    ):
        table.drop(bind, checkfirst=True)
