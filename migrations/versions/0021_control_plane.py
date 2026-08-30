"""T18b durable control commands and immutable project generation runs.

Purely additive: two new tables and nothing touched that T01-T25 created, so
every existing project, workflow run, render, QA result, approval, provider
attempt, cost record and publication is preserved untouched. The tables are
created from the same ORM metadata the application uses, so the migration and
the models cannot drift.

The constraints these tables install are the point of T18b, not decoration:

* ``control_command_dispatched_identity`` refuses to store a ``running``,
  ``awaiting_review`` or ``completed`` command that does not name a workflow.
  A calculated-but-never-started workflow ID is rejected by the database, which
  is what turns "an accepted command is durably dispatched" from a convention
  into an invariant.
* ``uq_control_commands_idempotency`` makes a command idempotent per project and
  type, so a duplicated submission adopts the first row rather than starting a
  second workflow or paying for a second provider attempt.
* ``control_command_active_claim`` keeps a claim and its lease together, so a
  killed dispatcher always leaves a recoverable row.
* ``uq_generation_run_active`` allows exactly one non-terminal generation run per
  project, so two concurrent revisions cannot both claim to be the project's
  active lineage while every historical run is preserved.

The downgrade refuses to run once commands or generation runs exist. These rows
are the only record of which workflow an accepted command actually started;
dropping them would leave running workflows with no owner and would let a replay
dispatch the same paid work again.

Revision ID: 0021_control_plane
Revises: 0020_youtube_publication
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import inspect

from vidgen.db.control_command_models import (
    ControlCommandRecord,
    ProjectGenerationRunRecord,
)

revision: str = "0021_control_plane"
down_revision: str | None = "0020_youtube_publication"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Generation runs first: a command may reference the run it opened.
_TABLES = (
    ProjectGenerationRunRecord.__table__,
    ControlCommandRecord.__table__,
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
            "unsafe T18b downgrade: control-plane provenance would be destroyed. These rows "
            "are the only record of which Temporal workflow an accepted command actually "
            "started, and of which generation run a project is currently executing; dropping "
            "them would orphan running workflows and let a replay dispatch the same paid work "
            "again. Export or delete rows from " + ", ".join(populated) + " before downgrading."
        )
    for table in reversed(_TABLES):
        table.drop(bind, checkfirst=True)
