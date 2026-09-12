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

This revision is a catch-up for databases that already have ``control_commands``,
not the column's only source: ``0021_control_plane`` builds that table from the
live model metadata, so a database created from empty already has the column and
this migration finds nothing to do. That is also why the downgrade leaves the
column alone. Dropping it would take it away from every revision back to 0021 -
including 0021's own downgrade guard, which reads the table through the same
model - and the column carries no provenance worth reclaiming: it records only
how long a command once waited for a cluster.

Revision ID: 0025_control_command_infrastructure_attempts
Revises: 0024_script_editing_passes
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025_control_command_infrastructure_attempts"
down_revision: str | None = "0024_script_editing_passes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "control_commands"
_COLUMN = "infrastructure_attempt"


def _already_added() -> bool:
    """Whether this database already has the column and must be left alone.

    ``0021_control_plane`` creates ``control_commands`` from the live model
    metadata, so a database built from empty arrives here with the column
    already in place and must not be asked to add it twice. Offline mode has no
    database to ask: it renders the script for a deployment that predates the
    column, which is the only case that needs the statement at all.
    """
    if op.get_context().as_sql:
        return False
    inspector = sa.inspect(op.get_bind())
    if _TABLE not in inspector.get_table_names():
        return True
    return any(column["name"] == _COLUMN for column in inspector.get_columns(_TABLE))


def upgrade() -> None:
    if _already_added():
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
