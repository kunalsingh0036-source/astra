"""
Approval store — the mechanism behind the autonomy system's ASK.

Lifecycle:
  1. The agent loop hits an ASK decision → create_approval() →
     a pending row + an approval_request event; the tool gets an
     is_error result ("awaiting approval #N") so the TURN NEVER
     BLOCKS on a human.
  2. Kunal resolves it — /approvals page, the resolve_approval chat
     tool, or WhatsApp ("approve 12") — resolve_approval() flips the
     row; standing=True also writes a tool_grants row.
  3. The next time the model calls the SAME tool, check_grant()
     consults: (a) standing tool_grants, (b) an unconsumed approved
     row for that tool — one-shot grants are marked consumed on use.

Everything is DB-backed (the same Postgres every service shares) —
no per-process state, which is the disease this subsystem's history
keeps re-teaching.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import text as _sql

from astra.db.engine import async_session

logger = logging.getLogger(__name__)

# Pending approvals older than this are expired by the daily
# retention sweep — a stale yes is not a yes.
EXPIRY_HOURS = 24


async def create_approval(
    *,
    tool_name: str,
    tool_input: dict[str, Any],
    reason: str,
    turn_id: int | None,
    session_id: str | None,
) -> int:
    """Insert a pending approval; returns its id."""
    async with async_session() as s:
        r = await s.execute(
            _sql(
                """
                INSERT INTO approvals
                    (turn_id, session_id, tool_name, tool_input, reason)
                VALUES (:t, :sid, :n, CAST(:i AS JSONB), :r)
                RETURNING id
                """
            ),
            {
                "t": turn_id,
                "sid": session_id,
                "n": tool_name,
                "i": json.dumps(tool_input)[:20_000],
                "r": reason[:2_000],
            },
        )
        approval_id = r.scalar_one()
        await s.commit()
    return int(approval_id)


async def check_grant(tool_name: str) -> tuple[bool, str]:
    """Is this tool currently allowed without asking?

    Returns (allowed, reason). Standing grants win; otherwise the
    OLDEST unconsumed approved row for the tool is consumed
    (one-shot). Best-effort: DB trouble = not granted (fail closed).
    """
    try:
        async with async_session() as s:
            r = await s.execute(
                _sql("SELECT 1 FROM tool_grants WHERE tool_name = :n"),
                {"n": tool_name},
            )
            if r.first():
                return True, "standing grant"
            r = await s.execute(
                _sql(
                    """
                    UPDATE approvals
                    SET status = 'consumed', resolved_at = now()
                    WHERE id = (
                        SELECT id FROM approvals
                        WHERE tool_name = :n AND status = 'approved'
                          -- standing approvals grant via tool_grants
                          -- ONLY; if they also matched here, revoking
                          -- the grant wouldn't actually revoke (the
                          -- approved row would keep feeding one-shots)
                          AND standing = false
                        ORDER BY created_at
                        LIMIT 1
                        FOR UPDATE SKIP LOCKED
                    )
                    RETURNING id
                    """
                ),
                {"n": tool_name},
            )
            row = r.first()
            await s.commit()
            if row:
                return True, f"one-shot approval #{row.id}"
    except Exception as e:
        logger.warning("[approvals] grant check failed: %s", e)
    return False, "no grant"


async def resolve_approval(
    approval_id: int,
    decision: str,
    *,
    standing: bool = False,
    source: str = "web",
) -> dict[str, Any]:
    """Flip a pending row to approved/denied. standing=True on an
    approval also writes a tool_grants row (per-tool auto-allow)."""
    if decision not in ("approved", "denied"):
        return {"ok": False, "error": "decision must be approved|denied"}
    async with async_session() as s:
        r = await s.execute(
            _sql(
                """
                UPDATE approvals
                SET status = :d, resolved_at = now(),
                    standing = :st, resolution_source = :src
                WHERE id = :id AND status = 'pending'
                RETURNING tool_name
                """
            ),
            {"d": decision, "st": standing, "src": source, "id": approval_id},
        )
        row = r.first()
        if row is None:
            await s.rollback()
            return {
                "ok": False,
                "error": f"approval #{approval_id} not found or not pending",
            }
        tool_name = row.tool_name
        if decision == "approved" and standing:
            await s.execute(
                _sql(
                    """
                    INSERT INTO tool_grants (tool_name, source, approval_id)
                    VALUES (:n, :src, :id)
                    ON CONFLICT (tool_name) DO UPDATE
                        SET granted_at = now(), source = :src,
                            approval_id = :id
                    """
                ),
                {"n": tool_name, "src": source, "id": approval_id},
            )
        await s.commit()
    return {"ok": True, "tool_name": tool_name, "decision": decision,
            "standing": standing}


async def list_pending(limit: int = 50) -> list[dict[str, Any]]:
    async with async_session() as s:
        r = await s.execute(
            _sql(
                """
                SELECT id, turn_id, tool_name, tool_input, reason, created_at
                FROM approvals WHERE status = 'pending'
                ORDER BY created_at DESC LIMIT :l
                """
            ),
            {"l": limit},
        )
        return [
            {
                "id": row.id,
                "turn_id": row.turn_id,
                "tool_name": row.tool_name,
                "tool_input": row.tool_input,
                "reason": row.reason,
                "created_at": row.created_at.isoformat(),
            }
            for row in r.fetchall()
        ]


async def revoke_grant(tool_name: str) -> bool:
    """Remove a standing grant — the demotion path on the trust ladder."""
    async with async_session() as s:
        r = await s.execute(
            _sql("DELETE FROM tool_grants WHERE tool_name = :n RETURNING tool_name"),
            {"n": tool_name},
        )
        found = r.first() is not None
        await s.commit()
    return found


async def expire_stale(hours: int = EXPIRY_HOURS) -> int:
    """Expire pending rows older than the window (retention sweep)."""
    async with async_session() as s:
        r = await s.execute(
            _sql(
                """
                UPDATE approvals SET status = 'expired', resolved_at = now()
                WHERE status = 'pending'
                  AND created_at < now() - make_interval(hours => :h)
                """
            ),
            {"h": hours},
        )
        await s.commit()
        return r.rowcount or 0
