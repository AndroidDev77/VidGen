"""T22 final editorial-QA runs, checks, provider attempts, reviews and gates.

The migration is purely additive: it introduces the five T22 tables and touches
nothing T01-T21 or T23 created, so existing projects, assets, renders, QA
results, repairs and cost records are preserved. Tables are created from the
same ORM metadata the application uses, which keeps the migration and the models
from drifting.

The downgrade refuses to run once final-QA provenance exists. A completion gate
is the record of why a project was - or was not - allowed to finish, and the
report it points at is immutable evidence for that decision; dropping either
would erase the audit trail and silently unblock a render that never passed.

Revision ID: 0018_final_editorial_qa
Revises: 0017_repair_fallback
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import inspect

from vidgen.db.final_editorial_models import (
    FinalCompletionGate,
    FinalEditorialCheckRecord,
    FinalEditorialProviderAttempt,
    FinalEditorialReview,
    FinalEditorialRun,
)

revision: str = "0018_final_editorial_qa"
down_revision: str | None = "0017_repair_fallback"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Creation order: the run first, then everything that references it.
_TABLES = (
    FinalEditorialRun.__table__,
    FinalEditorialCheckRecord.__table__,
    FinalEditorialProviderAttempt.__table__,
    FinalEditorialReview.__table__,
    FinalCompletionGate.__table__,
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
            "unsafe T22 downgrade: final editorial-QA provenance would be destroyed. The "
            "completion gate records why a project was or was not allowed to finish, and "
            "the immutable report it references is the evidence behind that decision. "
            "Export or delete rows from " + ", ".join(populated) + " before downgrading."
        )
    for table in reversed(_TABLES):
        table.drop(bind, checkfirst=True)
