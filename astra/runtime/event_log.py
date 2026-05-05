"""
Per-turn durable event log.

Every event the lean runtime yields gets written to the turn_events
table BEFORE being yielded to SSE. This makes events durable —
browsers polling /api/turns/<id>/events see the full sequence even
if they reconnect mid-turn, even if the stream service container
restarts, even if the SSE connection was never opened in the first
place.

Replaces the streaming-only model with a write-then-yield model.

API:
    record_event(turn_id, name, payload) -> int (returns ord)
    list_events(turn_id, after_ord=0) -> list[dict]
    next_ord_for(turn_id) -> int
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import text

from astra.db.engine import async_session

logger = logging.getLogger(__name__)


async def record_event(
    turn_id: int | None,
    event_name: str,
    payload: dict[str, Any] | None = None,
) -> int:
    """Append one event to turn_events. Returns the assigned ord.

    No-op when turn_id is None (legacy paths that don't have one).
    Swallows DB errors — the live SSE yield is the guaranteed path,
    the durable log is the belt-and-suspenders. Returns 0 on error.
    """
    if turn_id is None:
        return 0
    payload = payload or {}
    try:
        async with async_session() as s:
            r = await s.execute(
                text(
                    """
                    INSERT INTO turn_events (turn_id, ord, event_name, payload)
                    VALUES (
                        :tid,
                        COALESCE(
                          (SELECT MAX(ord) + 1 FROM turn_events WHERE turn_id = :tid),
                          1
                        ),
                        :name,
                        CAST(:p AS JSONB)
                    )
                    RETURNING ord
                    """
                ),
                {
                    "tid": int(turn_id),
                    "name": event_name[:31],
                    "p": json.dumps(payload, default=str),
                },
            )
            row = r.one()
            await s.commit()
            return int(row[0])
    except Exception:
        logger.exception(
            "[event-log] record failed for turn=%s event=%s",
            turn_id,
            event_name,
        )
        return 0


async def list_events(
    turn_id: int, *, after_ord: int = 0, limit: int = 500
) -> list[dict[str, Any]]:
    """Read events for one turn after a cursor. Returns chronological."""
    try:
        async with async_session() as s:
            r = await s.execute(
                text(
                    """
                    SELECT ord, event_name, payload, created_at
                    FROM turn_events
                    WHERE turn_id = :tid AND ord > :after
                    ORDER BY ord ASC
                    LIMIT :lim
                    """
                ),
                {
                    "tid": int(turn_id),
                    "after": int(after_ord),
                    "lim": int(limit),
                },
            )
            rows = r.all()
    except Exception:
        logger.exception("[event-log] list failed for turn=%s", turn_id)
        return []

    out: list[dict[str, Any]] = []
    for row in rows:
        ord_, name, payload, created_at = row
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                payload = {}
        out.append(
            {
                "ord": int(ord_),
                "event": str(name),
                "payload": payload or {},
                "created_at": (
                    created_at.isoformat()
                    if hasattr(created_at, "isoformat")
                    else str(created_at)
                ),
            }
        )
    return out


async def next_ord_for(turn_id: int) -> int:
    """Helper: what ord would the next event have? Used by writers
    that want the value before inserting (mostly tests)."""
    try:
        async with async_session() as s:
            r = await s.execute(
                text(
                    "SELECT COALESCE(MAX(ord), 0) + 1 FROM turn_events "
                    "WHERE turn_id = :tid"
                ),
                {"tid": int(turn_id)},
            )
            row = r.first()
            return int(row[0]) if row else 1
    except Exception:
        return 1
