"""Migrations: head applies cleanly, re-running is a no-op, and the models match the migrations (no drift)."""

from __future__ import annotations

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.migrate import upgrade_head
from app.db.tables import Base

PLAN_TABLES = {
    "connections", "destinations", "rule_sets", "content_items", "publications", "publish_intents", "jobs",
    "idempotency_keys", "certification_runs", "level_history", "alerts", "circuit_state", "rate_limit_state",
    "connector_flags", "test_posts", "catalogue_imports", "metrics_snapshots", "labels", "scheduled_tasks",
}


async def test_upgrade_is_idempotent_and_models_match(pg_url):
    engine = create_async_engine(pg_url)
    try:
        await upgrade_head(engine)  # already at head (template): must be a no-op
        await upgrade_head(engine)
        async with engine.connect() as conn:
            tables = set(await conn.run_sync(lambda c: inspect(c).get_table_names()))
            diff = await conn.run_sync(
                lambda c: compare_metadata(MigrationContext.configure(c, opts={"compare_type": True}), Base.metadata)
            )
            version = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar()
        assert PLAN_TABLES <= tables
        assert diff == [], f"models and migrations differ; add a revision: {diff}"
        assert version is not None
    finally:
        await engine.dispose()
