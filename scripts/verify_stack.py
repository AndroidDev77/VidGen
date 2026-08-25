from __future__ import annotations

from alembic import command
from alembic.config import Config
from sqlalchemy import text

from vidgen.db.session import build_engine


def main() -> None:
    engine = build_engine()
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    config = Config("alembic.ini")
    command.upgrade(config, "head")
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    print("PostgreSQL reachable; Alembic up/down/up succeeded")


if __name__ == "__main__":
    main()
