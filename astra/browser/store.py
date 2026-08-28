"""Browser task queue — Astra's hands inside Kunal's real Chrome.

Why a QUEUE and not a live connection: MV3 service workers are killed
after ~30s idle and chrome.alarms fires at most once a minute, so a
persistent socket has to be nursed with keepalives forever. Polling
inverts that — the extension asks for work when it is awake, and an
asleep browser is simply a browser with no hands right now, which is
the same honest degradation the Mac bridge already uses.

Safety model, deliberately narrow:
  - READ tasks (extract, screenshot, read_page) run unattended.
  - ACT tasks (click, type, navigate) are ALWAYS staged unapproved
    and are released only when the linked row in `approvals` has been
    resolved by a human (the /approvals page, the resolve_approval
    chat tool, or "approve N" on WhatsApp). Callers cannot pre-approve
    their own actions: `enqueue()` takes no approval argument, and
    `claim_next` reads the approvals table rather than a local flag.
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
    "ALTER TABLE browser_tasks ADD COLUMN IF NOT EXISTS approval_id INTEGER",
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
    ttl_minutes: int = 30, requested_by: str = "astra",
) -> dict[str, Any]:
    """Stage a browser task.

    There is deliberately NO `approved` parameter. The endpoint used to
    take one straight from the request body, so anything holding the
    mesh secret — including the agent loop itself, which reaches its own
    HTTP surface — could stage a click and approve it in the same call.
    The docstring claimed "only a human can set" while the code let the
    caller assert it. An ACT task now opens a row in `approvals`, and
    only resolve_approval() (the /approvals page, the chat tool, or
    "approve N" over WhatsApp) can release it.
    """
    if kind not in ALL_KINDS:
        raise ValueError(f"unknown browser task kind: {kind}")
    await ensure_tables()
    tid = uuid.uuid4()

    approval_id: int | None = None
    if kind in ACT_KINDS:
        from astra.autonomy.approvals import create_approval
        approval_id = await create_approval(
            tool_name=f"browser_{kind}",
            tool_input={"kind": kind, "url_pattern": url_pattern[:900],
                        "payload": payload or {}, "task_id": str(tid)},
            reason=(f"Astra wants to {kind} in your browser"
                    + (f" on {url_pattern[:120]}" if url_pattern else "")),
            turn_id=None,
            session_id=None,
        )
        logger.info("[browser] staged act task %s -> approval #%s",
                    kind, approval_id)

    async with async_session() as s:
        await s.execute(text("""
            INSERT INTO browser_tasks
                (id, kind, url_pattern, payload, approved, approval_id,
                 requested_by, expires_at)
            VALUES (:id, :k, :u, CAST(:p AS JSONB), FALSE, :ap, :rb, :exp)
        """), {
            "id": tid, "k": kind, "u": url_pattern[:900],
            "p": json.dumps(payload or {}), "ap": approval_id,
            "rb": requested_by,
            "exp": datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes),
        })
        await s.commit()
    return {"id": str(tid), "kind": kind, "approved": False,
            "approval_id": approval_id}


async def claim_next(url_hint: str = "") -> dict[str, Any] | None:
    """Hand the extension one task.

    An ACT task is released only when its linked `approvals` row reads
    'approved'. The gate is a JOIN, not a boolean on this row: the only
    writer of that status is resolve_approval(), which a human drives.
    The legacy `approved` column is no longer consulted — a task cannot
    talk its own way past the gate."""
    await ensure_tables()
    async with async_session() as s:
        row = (await s.execute(text("""
            UPDATE browser_tasks SET status='claimed', claimed_at=now()
            WHERE id = (
                SELECT id FROM browser_tasks
                WHERE status='pending'
                  AND expires_at > now()
                  AND (
                        kind = ANY(:read_kinds)
                        OR EXISTS (
                            SELECT 1 FROM approvals a
                            WHERE a.id = browser_tasks.approval_id
                              AND a.status = 'approved'
                        )
                      )
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
