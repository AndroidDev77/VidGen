"""Read-only helpers for reasoning about the applied Alembic revision.

Kept out of the migration script so the smoke test can assert the schema state
without importing anything that is able to change it. Nothing in this module
applies, upgrades or downgrades a migration.
"""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection

#: A fixed, arbitrary 64-bit key held by the migration job for the duration of a
#: run. Every runner in every environment uses the same one, so two of them
#: contend rather than proceeding in parallel. Nothing else in the application
#: takes an advisory lock, so there is no collision to avoid.
MIGRATION_ADVISORY_LOCK_KEY = 4_711_070_524_113_001


def repository_root() -> Path:
    return Path(__file__).resolve().parents[3]


def alembic_config(root: Path | None = None) -> Config:
    resolved = root or repository_root()
    config = Config(str(resolved / "alembic.ini"))
    config.set_main_option("script_location", str(resolved / "migrations"))
    return config


def script_heads(config: Config) -> tuple[str, ...]:
    """Every head in the migration script directory. More than one is a defect."""
    return tuple(ScriptDirectory.from_config(config).get_heads())


def database_revisions(connection: Connection) -> tuple[str, ...]:
    """The revisions currently stamped on the database."""
    return tuple(MigrationContext.configure(connection).get_current_heads())
