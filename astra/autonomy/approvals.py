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


# CONTAINMENT §6 / CHARTER no-standing list.
#
# Some capabilities must be approved EVERY time, per call. A standing
# grant on them is a permanent, argument-blind bypass: check_grant()
# compares only the tool NAME, so one "approve 9 always" on local_bash
# in June authorises every shell command on Kunal's Mac forever. That
# grant was found live in production on 2026-08-28 (tool_grants row,
# source="chat", granted 2026-06-12).
#
# These tools may still be approved — one call at a time — but a
# standing grant is refused at write time AND ignored at read time, so
# a row that predates this rule (or is inserted by any other path)
# cannot authorise anything.
#
# The list follows the charter: shell, anything that sends or
# publishes, anything that deletes, anything that changes autonomy or
# permissions, anything that edits/commits Astra's own code, and
# anything that exposes the machine.
NO_STANDING_TOOLS: frozenset[str] = frozenset({
    # arbitrary execution
    "local_bash",
    "Bash",
    "run_creator_tests",
    # sends / publishes
    "send_reply_draft",
    "approve_content_draft",
    "send_a2a_task",
    # self-modification + deploy
    "edit_astra_file",
    "write_astra_file",
    "local_edit",
    "local_write",
    "commit_code_changes",
    "commit_kit_changes",
    "revert_last_code_commit",
    "apply_self_improvement",
    # deletes / destructive ops
    "forget_memory",
    "restart_agent",
    "cancel_a2a_task",
    # permission + autonomy surface
    "set_mode",
    "resolve_approval",
    "revoke_tool_grant",
    # exposes the machine
    "start_tunnel",
    "stop_tunnel",
})


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
                if tool_name in NO_STANDING_TOOLS:
                    # A row exists but must not authorise anything —
                    # this tool is approved per call or not at all.
                    logger.warning(
                        "[approvals] IGNORING standing grant for %s — "
                        "on the no-standing list (CONTAINMENT §6)",
                        tool_name,
                    )
                else:
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
    standing_requested = standing
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
        if standing and tool_name in NO_STANDING_TOOLS:
            # Downgrade to a one-shot rather than failing the whole
            # resolution: the human said yes to THIS call, which is
            # still valid. What is refused is the "and every future
            # one" part.
            logger.warning(
                "[approvals] refusing STANDING grant for %s — "
                "no-standing list; resolving as one-shot instead",
                tool_name,
            )
            standing = False
            await s.execute(
                _sql(
                    "UPDATE approvals SET standing = false WHERE id = :id"
                ),
                {"id": approval_id},
            )
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
            "standing": standing,
            "standing_refused": bool(standing_requested and not standing)}


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
