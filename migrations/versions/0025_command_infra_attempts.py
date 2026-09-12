"""Give control commands a second, separate budget for infrastructure failures.

One column: ``control_commands.infrastructure_attempt``. It counts the times a
command has been held over a failure of the control plane's own dependencies -
an unreachable Temporal cluster, a query no worker could answer in time - as
distinct from ``attempt``, which counts what the command itself has tried.

The distinction is the point. Before this column the two shared one budget, so a
cluster hiccup lasting half a minute walked a user's approve to ``5 of 5`` and
killed it permanently, having learned nothing about whether the command was any
good. With a budget of its own the dispatcher can hand the attempt back and wait
the outage out on a longer backoff, and still settle the command if whatever was
down never comes back.

Existing rows start at zero, which is exactly right: they have not been deferred.

This revision is a catch-up, not the column's only source. ``0021_control_plane``
builds ``control_commands`` from the live model metadata, so a database created
from empty already arrives here with the column. That single fact shapes
everything below:

* on PostgreSQL the statement is written ``ADD COLUMN IF NOT EXISTS``, so it is
  correct whether or not 0021 already created the column - including in offline
  ``--sql`` rendering, which has no database to ask and would otherwise emit an
  ``ALTER`` for a column the same script had just created;
* elsewhere - SQLite, which this project uses only for tests, all of which run
  against a live connection - the column is added only after inspecting for it;
* the downgrade leaves the column alone. Dropping it would take it away from
  every revision back to 0021, including 0021's own downgrade guard, which reads
  the table through the same model. The column carries no provenance worth
  reclaiming: it records only how long a command once waited for a cluster.

Revision ID: 0025_command_infra_attempts
Revises: 0024_script_editing_passes
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025_command_infra_attempts"
down_revision: str | None = "0024_script_editing_passes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "control_commands"
_COLUMN = "infrastructure_attempt"


def _already_added() -> bool:
    """Whether a live database already has the column, so nothing is to be done."""
    inspector = sa.inspect(op.get_bind())
    if _TABLE not in inspector.get_table_names():
        return True
    return any(column["name"] == _COLUMN for column in inspector.get_columns(_TABLE))


def upgrade() -> None:
    context = op.get_context()
    if context.dialect.name == "postgresql":
        op.execute(
            f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} INTEGER NOT NULL DEFAULT 0"
        )
        return
    if context.as_sql or _already_added():
        # No connection to inspect, or nothing to add. A rendered script for a
        # dialect without ``IF NOT EXISTS`` cannot know which it is, and the
        # column is already present on every database this project builds.
        return
    op.add_column(
        _TABLE,
        sa.Column(_COLUMN, sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    """Deliberately nothing. See the module docstring.

    The column belongs to the table ``0021_control_plane`` creates; this
    revision only backfills it onto databases that predate it. Dropping it here
    would leave every earlier revision with a table the model cannot read.
    """
