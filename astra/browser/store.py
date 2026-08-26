"""Browser task queue — Astra's hands inside Kunal's real Chrome.

Why a QUEUE and not a live connection: MV3 service workers are killed
after ~30s idle and chrome.alarms fires at most once a minute, so a
persistent socket has to be nursed with keepalives forever. Polling
inverts that — the extension asks for work when it is awake, and an
asleep browser is simply a browser with no hands right now, which is
the same honest degradation the Mac bridge already uses.

Safety model, deliberately narrow:
  - READ tasks (extract, screenshot, read_page) run unattended.
  - ACT tasks (click, type, navigate) require `approved=true`, which
    only a human can set. The extension refuses anything else.
  - There is NO task type that submits a form, sends a message, or
    completes a payment. Draft-don't-send holds inside the browser too.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from astra.db.engine import async_session

logger = logging.getLogger(__name__)

# Anything that changes what the page shows or does. Never auto-runs.
ACT_KINDS = {"click", "type", "navigate", "scroll"}
READ_KINDS = {"read_page", "extract", "screenshot", "list_tabs"}
ALL_KINDS = ACT_KINDS | READ_KINDS

_ENSURE = [
    """
    CREATE TABLE IF NOT EXISTS browser_tasks (
        id           UUID PRIMARY KEY,
        kind         VARCHAR(24) NOT NULL,
        url_pattern  TEXT NOT NULL DEFAULT '',
        payload      JSONB NOT NULL DEFAULT '{}'::jsonb,
        approved     BOOLEAN NOT NULL DEFAULT FALSE,
        status       VARCHAR(16) NOT NULL DEFAULT 'pending',
        result       JSONB,
        error        TEXT,
        requested_by VARCHAR(40) NOT NULL DEFAULT 'astra',
        created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
        claimed_at   TIMESTAMPTZ,
        done_at      TIMESTAMPTZ,
        expires_at   TIMESTAMPTZ NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_browser_tasks_pending ON browser_tasks (status, created_at)",
]

_ensured = False


async def ensure_tables() -> None:
    global _ensured
    if _ensured:
        return
    async with async_session() as s:
        for stmt in _ENSURE:
            await s.execute(text(stmt))
        await s.commit()
    _ensured = True


async def enqueue(
    *, kind: str, url_pattern: str = "", payload: dict | None = None,
    approved: bool = False, ttl_minutes: int = 30,
    requested_by: str = "astra",
) -> dict[str, Any]:
    if kind not in ALL_KINDS:
        raise ValueError(f"unknown browser task kind: {kind}")
    if kind in ACT_KINDS and not approved:
        # Not an error — a staged action awaiting a human. Recorded so the
        # approval surface can show it.
        logger.info("[browser] staging UNAPPROVED act task: %s", kind)
    await ensure_tables()
    tid = uuid.uuid4()
    async with async_session() as s:
        await s.execute(text("""
            INSERT INTO browser_tasks
                (id, kind, url_pattern, payload, approved, requested_by, expires_at)
            VALUES (:id, :k, :u, CAST(:p AS JSONB), :a, :rb, :exp)
        """), {
            "id": tid, "k": kind, "u": url_pattern[:900],
            "p": json.dumps(payload or {}), "a": approved, "rb": requested_by,
            "exp": datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes),
        })
        await s.commit()
    return {"id": str(tid), "kind": kind, "approved": approved}


async def claim_next(url_hint: str = "") -> dict[str, Any] | None:
    """Hand the extension one task. ACT tasks are only ever handed over
    once a human has approved them — the gate lives here, server-side,
    so a compromised or modified extension cannot grant itself one."""
    await ensure_tables()
    async with async_session() as s:
        row = (await s.execute(text("""
            UPDATE browser_tasks SET status='claimed', claimed_at=now()
            WHERE id = (
                SELECT id FROM browser_tasks
                WHERE status='pending'
                  AND expires_at > now()
                  AND (kind = ANY(:read_kinds) OR approved = TRUE)
                ORDER BY created_at ASC
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            RETURNING id, kind, url_pattern, payload
        """), {"read_kinds": list(READ_KINDS)})).mappings().first()
        await s.commit()
    if not row:
        return None
    return {"id": str(row["id"]), "kind": row["kind"],
            "url_pattern": row["url_pattern"], "payload": row["payload"]}


async def complete(task_id: str, *, result: dict | None = None,
                   error: str = "") -> bool:
    async with async_session() as s:
        r = await s.execute(text("""
            UPDATE browser_tasks
            SET status = CASE WHEN :err = '' THEN 'done' ELSE 'error' END,
                result = CAST(:res AS JSONB), error = :err, done_at = now()
            WHERE id = CAST(:id AS UUID) RETURNING id
        """), {"id": task_id, "res": json.dumps(result or {}), "err": error[:2000]})
        ok = r.scalar() is not None
        await s.commit()
    return ok


async def get_task(task_id: str) -> dict[str, Any] | None:
    await ensure_tables()
    async with async_session() as s:
        row = (await s.execute(text(
            "SELECT * FROM browser_tasks WHERE id = CAST(:id AS UUID)"),
            {"id": task_id})).mappings().first()
    return dict(row) if row else None


async def expire_stale() -> int:
    """A task nobody claimed is not a task that ran. Mark it, loudly."""
    async with async_session() as s:
        r = await s.execute(text("""
            UPDATE browser_tasks
            SET status='expired',
                error='browser was not available before the task expired'
            WHERE status IN ('pending','claimed') AND expires_at < now()
            RETURNING id
        """))
        n = len(r.fetchall())
        await s.commit()
    return n
