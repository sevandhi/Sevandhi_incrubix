"""Apply Alembic migrations, guarded by a Postgres advisory lock so api and worker never race.

Usage:
    uv run python -m app.db.migrate                      # upgrade to head (DATABASE_URL from settings)
    uv run python -m app.db.migrate revision -m "msg"    # autogenerate a new revision (needs a migrated DB)
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.session import advisory_key

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
MIGRATION_LOCK = advisory_key("ibx:migrations")


def alembic_config(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


async def upgrade_head(engine: AsyncEngine) -> None:
    """Upgrade to head on one connection that holds the migration advisory lock for the whole run."""
    url = engine.url.render_as_string(hide_password=False)

    def _upgrade(sync_conn) -> None:  # noqa: ANN001
        cfg = alembic_config(url)
        cfg.attributes["connection"] = sync_conn
        command.upgrade(cfg, "head")

    async with engine.connect() as conn:
        await conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": MIGRATION_LOCK})
        await conn.commit()
        try:
            await conn.run_sync(_upgrade)
            await conn.commit()
        finally:
            await conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": MIGRATION_LOCK})
            await conn.commit()


async def _main_upgrade(url: str) -> None:
    from app.db.session import make_engine

    engine = make_engine(url, 1)  # accepts postgres:// and ?sslmode=require like the app does
    try:
        await upgrade_head(engine)
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> None:
    from app.config import get_settings

    parser = argparse.ArgumentParser(prog="python -m app.db.migrate")
    parser.add_argument("action", nargs="?", default="upgrade", choices=["upgrade", "revision"])
    parser.add_argument("-m", "--message", default="change")
    parser.add_argument("--url", default=None)
    args = parser.parse_args(argv)
    url = args.url or get_settings().DATABASE_URL
    if args.action == "upgrade":
        asyncio.run(_main_upgrade(url))
        print("migrations: at head")
    else:
        command.revision(alembic_config(url), message=args.message, autogenerate=True)


if __name__ == "__main__":
    main()
