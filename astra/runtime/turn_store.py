"""
Turn-record persistence — durable per-turn rows in the `turns` table.

Every chat turn writes a row at turn-start (status='running') and
updates it at turn-end (status='complete' / 'failed' / 'interrupted').
This is the recovery anchor: even if the SSE stream dies mid-turn,
the prompt + session_id are durable in Postgres, so the user (or a
future cleanup job) can identify orphaned work.

Moved here from services/stream/runner.py during Phase 6 of the
lean-runtime migration. The stream service now imports from
astra.runtime.turn_store instead of the (now-deleted) SDK runner.

All DB writes SWALLOW exceptions — turn-record failures must never
break the user's actual turn.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text

from astra.db.engine import async_session

logger = logging.getLogger(__name__)


async def create_turn_record(
    *, session_id: str | None, prompt: str
) -> int | None:
    """Insert a 'running' turn row and return its id.

    Returns None on any error (table missing, DB down, etc.) — the
    caller continues fine without the record.
    """
    try:
        async with async_session() as s:
            r = await s.execute(
                text(
                    """
                    INSERT INTO turns (session_id, prompt, status)
                    VALUES (:sid, :p, 'running')
                    RETURNING id
                    """
                ),
                {"sid": (session_id or None), "p": prompt[:65000]},
            )
            row = r.one()
            await s.commit()
            return int(row[0])
    except Exception:
        logger.exception("[turns] failed to create running row")
        return None


async def finalize_turn_record(
    turn_id: int | None,
    *,
    session_id: str | None,
    response: str,
    status: str,
    duration_ms: int,
    cost_usd: float | None,
    tool_count: int,
    error_message: str | None = None,
) -> None:
    """Update a turn row with its final state. No-op when turn_id is None."""
    if turn_id is None:
        return
    try:
        async with async_session() as s:
            await s.execute(
                text(
                    """
                    UPDATE turns
                    SET response = :r,
                        status = :st,
                        duration_ms = :d,
                        cost_usd = :c,
                        tool_count = :tc,
                        error_message = :em,
                        ended_at = now(),
                        session_id = COALESCE(session_id, :sid)
                    WHERE id = :id
                    """
                ),
                {
                    "id": int(turn_id),
                    "r": (response or "")[:262144],  # ~256KB cap
                    "st": status[:15],
                    "d": int(duration_ms),
                    "c": cost_usd,
                    "tc": int(tool_count),
                    "em": (error_message or None) and error_message[:4000],
                    "sid": session_id,
                },
            )
            await s.commit()
    except Exception:
        logger.exception("[turns] failed to finalize id=%s", turn_id)


# ── Backwards-compat aliases ──────────────────────────────
#
# Some early call sites used the leading-underscore names from when
# these helpers lived inside services/stream/runner.py. Keep aliases
# so the migration is import-only — no signature changes for callers.

_create_turn_record = create_turn_record
_finalize_turn_record = finalize_turn_record
