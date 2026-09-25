"""Execute the actual reaper SQL in in-memory SQLite; no network/production DB.

Only now() is supplied as a deterministic test clock. This query's UPDATE,
COALESCE and predicates have the same semantics in SQLite and Postgres.
"""
import asyncio
import sqlite3

from astra.broker import store


def test_expiry_preserves_unknown_effect_and_does_not_retry(monkeypatch):
    database = sqlite3.connect(":memory:")
    database.create_function("now", 0, lambda: 200)
    database.execute("""CREATE TABLE intents (
        id INTEGER PRIMARY KEY, status TEXT, expires_at INTEGER,
        resolved_at INTEGER, deny_reason TEXT, receipt_bytes BLOB,
        result_bytes BLOB
    )""")
    states = ("pending", "claimed", "awaiting_human", "running", "succeeded",
              "failed", "denied", "expired")
    database.executemany(
        "INSERT INTO intents(id,status,expires_at,receipt_bytes,result_bytes) VALUES (?,?,100,?,?)",
        [(i, state, b"synthetic receipt", b"synthetic result")
         for i, state in enumerate(states, 1)],
    )
    database.execute("INSERT INTO intents VALUES (9,'running',300,NULL,NULL,NULL,NULL)")
    database.execute("INSERT INTO intents VALUES (10,'running',100,NULL,'existing evidence',NULL,NULL)")
    commits = []

    class LocalSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def execute(self, statement, parameters):
            return database.execute(str(statement), parameters)

        async def commit(self):
            database.commit()
            commits.append(True)

    monkeypatch.setattr(store._engine, "async_session", LocalSession)
    try:
        assert asyncio.run(store.expire_stale_intents()) == 5
        assert commits == [True]
        rows = database.execute("SELECT * FROM intents ORDER BY id").fetchall()
        for row in rows[:4]:
            _, status, _, resolved, reason, receipt, result = row
            assert status == "expired" and resolved == 200
            assert "completion was not confirmed" in reason
            assert "may have executed" in reason
            assert "Do not retry automatically" in reason
            assert "did not complete" not in reason
            assert receipt == b"synthetic receipt" and result == b"synthetic result"
        # Terminal evidence, unexpired work and an existing reason stay intact.
        for row, state in zip(rows[4:8], states[4:]):
            assert row[1] == state and row[3] is None and row[4] is None
        assert rows[8][1] == "running" and rows[8][3] is None
        assert rows[9][4] == "existing evidence"
        # Reaping again never creates a retry or reopens a terminal row.
        assert asyncio.run(store.expire_stale_intents()) == 0
        assert database.execute("SELECT count(*) FROM intents").fetchone()[0] == 10
    finally:
        database.close()
