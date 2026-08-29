"""Apply Alembic migrations exactly once, safely, from a dedicated job.

This is the only place schema changes are applied. Neither the API nor the
Temporal worker runs a migration at startup: a schema change has to be a
deliberate, observable, single-writer step that either succeeds before any new
application revision receives traffic, or fails and blocks the deployment.

Three properties are enforced here rather than assumed:

* **One writer.** A PostgreSQL session-level advisory lock is taken before
  Alembic is invoked. Container Apps already caps the migration job at
  parallelism one, but a manual `az containerapp job start` alongside a running
  deployment is exactly the case a platform setting does not cover.
* **One head.** After the upgrade the script asserts the script directory has
  exactly one head and that the database is at it. Two heads mean two branches
  of history were merged without a merge revision, and the next deployment would
  apply them in an arbitrary order.
* **No downgrade.** There is no code path in this script that calls
  ``alembic downgrade``. A rollback restores the previous application revision;
  it never destroys data.

Nothing here logs a connection string, a password or any row content.
"""

from __future__ import annotations

import argparse
import logging
import sys

from alembic import command
from sqlalchemy import text

from vidgen.db.alembic_state import (
    MIGRATION_ADVISORY_LOCK_KEY,
    alembic_config,
    database_revisions,
    script_heads,
)
from vidgen.db.session import build_engine
from vidgen.telemetry.bootstrap import initialize_telemetry

_LOGGER = logging.getLogger("vidgen.migrations")


def upgrade_to_head() -> int:
    """Return a process exit code: 0 on success, non-zero on any failure."""
    config = alembic_config()
    heads = script_heads(config)
    if len(heads) != 1:
        # Checked before anything is applied: a branched history must never be
        # partially applied and then reported as a deployment failure.
        _LOGGER.error(
            "refusing to migrate: the script directory has %d heads, expected exactly 1",
            len(heads),
        )
        return 2

    engine = build_engine()
    with engine.connect() as connection:
        # Session-scoped, not transaction-scoped: Alembic runs its own
        # transactions, and the lock has to outlive them. It is released when
        # this connection closes, including if the process is killed.
        acquired = connection.exec_driver_sql(
            "SELECT pg_try_advisory_lock(%s)", (MIGRATION_ADVISORY_LOCK_KEY,)
        ).scalar()
        if not acquired:
            _LOGGER.error(
                "another migration run holds the advisory lock; refusing to run concurrently"
            )
            return 3
        try:
            before = database_revisions(connection)
            _LOGGER.info("applying migrations", extra={"fromRevision": ",".join(before) or "base"})
            # Alembic opens its own connections from the same engine URL. The
            # lock above is held on this separate connection for the duration.
            command.upgrade(config, "head")
            # End this connection's implicit transaction so the re-read sees the
            # rows Alembic committed on its own connections. A session-level
            # advisory lock is not released by a rollback, so the lock still
            # covers the verification below.
            connection.rollback()
            after = database_revisions(connection)
            if after != heads:
                _LOGGER.error(
                    "database is at %s after upgrade but the single script head is %s",
                    ",".join(after) or "base",
                    heads[0],
                )
                return 4
            _LOGGER.info("migrations applied", extra={"toRevision": heads[0]})
            return 0
        finally:
            connection.exec_driver_sql(
                "SELECT pg_advisory_unlock(%s)", (MIGRATION_ADVISORY_LOCK_KEY,)
            )


def verify_single_head() -> int:
    """Assert, without changing anything, that the database is at the one head."""
    config = alembic_config()
    heads = script_heads(config)
    if len(heads) != 1:
        _LOGGER.error("the script directory has %d heads, expected exactly 1", len(heads))
        return 2
    engine = build_engine()
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
        current = database_revisions(connection)
    if current != heads:
        _LOGGER.error(
            "database is at %s but the single script head is %s",
            ",".join(current) or "base",
            heads[0],
        )
        return 4
    _LOGGER.info("database is at the single Alembic head", extra={"toRevision": heads[0]})
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--upgrade-head",
        action="store_true",
        help="apply every pending migration and verify the result",
    )
    group.add_argument(
        "--verify-head",
        action="store_true",
        help="verify the database is at the single head without changing it",
    )
    arguments = parser.parse_args(argv)
    initialize_telemetry(service_name="vidgen-migration")
    if arguments.verify_head:
        return verify_single_head()
    return upgrade_to_head()


if __name__ == "__main__":
    sys.exit(main())
