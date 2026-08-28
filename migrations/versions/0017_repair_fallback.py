"""T21 repair runs, attempts, decisions, fallback renders and Veo operations.

The migration is purely additive: it introduces the five T21 tables and touches
nothing T01-T20 or T23 created, so existing projects, assets, animations, QA
results and cost records are preserved. Tables are created from the same ORM
metadata the application uses, which keeps the migration and the models from
drifting.

The downgrade refuses to run once repair provenance exists. A repair attempt is
the evidence of what a project was charged for and why a shot was routed to a
human; dropping it would erase both the lineage and the cost trail.

Revision ID: 0017_repair_fallback
Revises: 0016_visual_qa
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import inspect

from vidgen.db.repair_models import (
    RepairAttemptRecord,
    RepairDecisionRecord,
    RepairFallbackRender,
    RepairRun,
    VeoOperationRecord,
)

revision: str = "0017_repair_fallback"
down_revision: str | None = "0016_visual_qa"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Creation order: parents before children. The self-referential
# ``repair_runs.selected_attempt_id`` is declared ``use_alter`` so the cycle
# between runs and attempts does not constrain this ordering.
_TABLES = (
    RepairRun.__table__,
    RepairAttemptRecord.__table__,
    RepairDecisionRecord.__table__,
    RepairFallbackRender.__table__,
    VeoOperationRecord.__table__,
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
            "unsafe T21 downgrade: repair provenance would be destroyed, and the attempt "
            "lineage proving what a project was charged for - and why a shot was routed to "
            "human review - would be lost. Export or delete rows from "
            + ", ".join(populated)
            + " before downgrading."
        )
    for table in reversed(_TABLES):
        table.drop(bind, checkfirst=True)
