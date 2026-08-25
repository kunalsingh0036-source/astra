"""Objective persistence + the point-of-use schema guard."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from astra.db.engine import async_session

logger = logging.getLogger(__name__)

# Identical SQL to the migration. Same discipline as
# astra/creators/engagement.py and astra/research/spine.py.
_ENSURE = [
    """
    CREATE TABLE IF NOT EXISTS objectives (
        id              SERIAL PRIMARY KEY,
        title           VARCHAR(300) NOT NULL,
        goal            TEXT NOT NULL DEFAULT '',
        done_when       JSONB NOT NULL DEFAULT '{}'::jsonb,
        channel         VARCHAR(20) NOT NULL DEFAULT 'none',
        target_ref      VARCHAR(200) NOT NULL DEFAULT '',
        context         JSONB NOT NULL DEFAULT '{}'::jsonb,
        state           VARCHAR(16) NOT NULL DEFAULT 'active',
        attempts        INTEGER NOT NULL DEFAULT 0,
        max_attempts    INTEGER NOT NULL DEFAULT 4,
        escalate_after  INTEGER NOT NULL DEFAULT 2,
        cadence_days    INTEGER NOT NULL DEFAULT 3,
        next_check_at   TIMESTAMPTZ NOT NULL,
        deadline_at     TIMESTAMPTZ,
        last_action_at  TIMESTAMPTZ,
        close_reason    TEXT NOT NULL DEFAULT '',
        closed_at       TIMESTAMPTZ,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_objectives_due ON objectives (state, next_check_at)",
    """
    CREATE TABLE IF NOT EXISTS objective_events (
        id           SERIAL PRIMARY KEY,
        objective_id INTEGER NOT NULL REFERENCES objectives(id) ON DELETE CASCADE,
        at           TIMESTAMPTZ NOT NULL DEFAULT now(),
        action       VARCHAR(24) NOT NULL,
        detail       TEXT NOT NULL DEFAULT '',
        artifact_id  INTEGER
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_objective_events_obj ON objective_events (objective_id, at DESC)",
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


async def create_objective(
    *, title: str, goal: str = "", done_when: dict | None = None,
    channel: str = "none", target_ref: str = "", context: dict | None = None,
    cadence_days: int = 3, max_attempts: int = 4, escalate_after: int = 2,
    deadline_at: datetime | None = None, start_in_days: int = 0,
) -> dict[str, Any]:
    await ensure_tables()
    now = datetime.now(timezone.utc)
    async with async_session() as s:
        row = (await s.execute(text("""
            INSERT INTO objectives
                (title, goal, done_when, channel, target_ref, context,
                 cadence_days, max_attempts, escalate_after,
                 next_check_at, deadline_at)
            VALUES (:t, :g, CAST(:dw AS JSONB), :ch, :tr, CAST(:cx AS JSONB),
                    :cad, :maxa, :esc, :nca, :dl)
            RETURNING id, title, state, next_check_at
        """), {
            "t": title[:300], "g": goal, "dw": json.dumps(done_when or {"kind": "manual"}),
            "ch": channel, "tr": target_ref[:200], "cx": json.dumps(context or {}),
            "cad": cadence_days, "maxa": max_attempts, "esc": escalate_after,
            "nca": now + timedelta(days=start_in_days), "dl": deadline_at,
        })).mappings().one()
        await s.commit()
    return dict(row)


async def due_objectives(limit: int = 50) -> list[dict[str, Any]]:
    await ensure_tables()
    async with async_session() as s:
        rows = (await s.execute(text("""
            SELECT * FROM objectives
            WHERE state = 'active'
            ORDER BY next_check_at ASC
            LIMIT :n
        """), {"n": limit})).mappings().all()
    return [dict(r) for r in rows]


async def list_objectives(state: str | None = None, limit: int = 40) -> list[dict[str, Any]]:
    await ensure_tables()
    sql = "SELECT * FROM objectives"
    params: dict = {"n": limit}
    if state:
        sql += " WHERE state = :st"
        params["st"] = state
    sql += " ORDER BY (state='active') DESC, next_check_at ASC LIMIT :n"
    async with async_session() as s:
        rows = (await s.execute(text(sql), params)).mappings().all()
    return [dict(r) for r in rows]


async def record_event(objective_id: int, action: str, detail: str = "",
                       artifact_id: int | None = None) -> None:
    async with async_session() as s:
        await s.execute(text("""
            INSERT INTO objective_events (objective_id, action, detail, artifact_id)
            VALUES (:o, :a, :d, :art)
        """), {"o": objective_id, "a": action[:24], "d": detail[:4000], "art": artifact_id})
        await s.commit()


async def advance(objective_id: int, *, attempts: int, next_check_at: datetime,
                  last_action_at: datetime) -> None:
    async with async_session() as s:
        await s.execute(text("""
            UPDATE objectives
            SET attempts = :a, next_check_at = :n, last_action_at = :l, updated_at = now()
            WHERE id = :id
        """), {"id": objective_id, "a": attempts, "n": next_check_at, "l": last_action_at})
        await s.commit()


async def close_objective(objective_id: int, *, state: str, reason: str) -> None:
    async with async_session() as s:
        await s.execute(text("""
            UPDATE objectives
            SET state = :st, close_reason = :r, closed_at = now(), updated_at = now()
            WHERE id = :id
        """), {"id": objective_id, "st": state, "r": reason[:2000]})
        await s.commit()
