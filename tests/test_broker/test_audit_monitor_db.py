"""Real SQL/migration checks in a private schema on loopback Postgres only."""
import importlib.util
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from astra.broker import audit_monitor as M


@pytest_asyncio.fixture
async def database(local_dsn, monkeypatch):
    # local_dsn is the repository's non-bypassable loopback fixture.
    schema = "audit_monitor_test_" + uuid4().hex
    admin = create_async_engine(local_dsn)
    engine = create_async_engine(local_dsn, connect_args={"server_settings": {"search_path": schema}})
    path = Path(__file__).resolve().parents[2] / "astra/db/migrations/versions/z4s70t5q1o3o_audit_monitor.py"
    spec = importlib.util.spec_from_file_location("audit_monitor_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    async with admin.begin() as c:
        await c.execute(text(f'CREATE SCHEMA "{schema}"'))
    try:
        def upgrade(c):
            with Operations.context(MigrationContext.configure(c)):
                migration.upgrade()
        async with engine.begin() as c:
            await c.run_sync(upgrade)
        monkeypatch.setattr(M.db, "async_session", async_sessionmaker(engine, expire_on_commit=False))
        yield engine, migration
    finally:
        await engine.dispose()
        async with admin.begin() as c:
            await c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


def observed(**kw):
    return M.Observation("verify", "ab" * 32, datetime.now(timezone.utc),
                         "failed", "gap", **kw)


async def test_real_sql_retains_notification_across_checks_and_resets_on_new_pin(database):
    o = observed(record_count=3, head_seq=3)
    assert (await M.save(o))["notified_key"] is None
    await M.mark_notified(o)
    later = replace(o, checked_at=o.checked_at + timedelta(seconds=1))
    assert (await M.save(later))["notified_key"] == o.alert_key
    assert M.notification(later, (await M.save(later))["notified_key"]) is None
    foreign = replace(later, chain_id="cd" * 32)
    assert (await M.save(foreign))["notified_key"] is None
    rows = await M.snapshots()
    assert len(rows) == 1 and rows[0]["chain_id"] == foreign.chain_id


async def test_late_check_and_late_notification_cannot_overwrite_new_state(database):
    old = observed()
    new = replace(old, checked_at=old.checked_at + timedelta(seconds=10), verdict="tampered")
    await M.save(new)
    assert await M.save(old) is None
    await M.mark_notified(old)
    assert (await M.save(new))["notified_key"] is None


async def test_sleep_preserves_last_delivered_incident_until_true_recovery(database):
    o = replace(observed(), check_kind="freshness", verdict="stale")
    await M.save(o)
    await M.mark_notified(o)
    asleep = replace(o, state="deferred", checked_at=o.checked_at + timedelta(seconds=1))
    row = await M.save(asleep)
    assert row["notified_key"] == o.alert_key
    assert M.notification(asleep, row["notified_key"]) is None
    recovered = replace(asleep, state="ok", verdict="fresh", checked_at=o.checked_at + timedelta(seconds=2))
    row = await M.save(recovered)
    assert M.notification(recovered, row["notified_key"])
    await M.mark_notified(recovered)
    assert (await M.save(recovered))["notified_key"] == "healthy"


async def test_migration_downgrade_only_removes_monitor_checkpoint(database):
    engine, migration = database
    def downgrade(c):
        with Operations.context(MigrationContext.configure(c)):
            migration.downgrade()
    async with engine.begin() as c:
        await c.run_sync(downgrade)
        assert (await c.execute(text("SELECT to_regclass('audit_verifications')"))).scalar() is None
