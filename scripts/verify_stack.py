from __future__ import annotations

import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import text

from vidgen.db.session import build_engine


def main() -> None:
    engine = build_engine()
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    if os.getenv("VIDGEN_ALLOW_DESTRUCTIVE_MIGRATION_TEST") != "1":
        raise RuntimeError(
            "refusing downgrade test without VIDGEN_ALLOW_DESTRUCTIVE_MIGRATION_TEST=1"
        )
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.upgrade(config, "head")
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    print("PostgreSQL reachable; Alembic up/down/up succeeded")


if __name__ == "__main__":
    main()
