"""T20 semantic visual QA runs, samples, attempts, results, evidence and reviews.

The migration is purely additive: it introduces the six T20 tables and touches
nothing T01-T19 or T23 created, so existing projects, assets, renders and cost
records are preserved. Tables are created from the same ORM metadata the
application uses, which keeps the migration and the models from drifting.

The downgrade refuses to run once QA provenance exists. A recorded QA result is
the evidence that a shot was blocked or cleared; silently dropping it would let
a previously failed shot become renderable again with no trace.

Revision ID: 0016_visual_qa
Revises: 0015_continuity_references
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import inspect

from vidgen.db.visual_qa_models import (
    VisualQAAttempt,
    VisualQAEvidenceRecord,
    VisualQAHumanReview,
    VisualQAResultRecord,
    VisualQARun,
    VisualQASampleRecord,
)

revision: str = "0016_visual_qa"
down_revision: str | None = "0015_continuity_references"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Creation order: parents before children. The self-referential
# ``visual_qa_runs.selected_result_id`` is declared ``use_alter`` so the cycle
# does not constrain this ordering.
_TABLES = (
    VisualQARun.__table__,
    VisualQASampleRecord.__table__,
    VisualQAAttempt.__table__,
    VisualQAResultRecord.__table__,
    VisualQAEvidenceRecord.__table__,
    VisualQAHumanReview.__table__,
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
            "unsafe T20 downgrade: visual-QA provenance would be destroyed, and shots "
            "blocked by a hard failure would silently become renderable again. Export or "
            "delete rows from " + ", ".join(populated) + " before downgrading."
        )
    for table in reversed(_TABLES):
        table.drop(bind, checkfirst=True)
