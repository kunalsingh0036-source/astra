"""
Local bridge — Postgres-backed token + call queue store.

All bridge state lives in two tables created by migration
p4i70j6h1e3e:
  bridge_tokens  — daemon credentials, allowed_paths, allowed_bash_patterns
  bridge_calls   — pending/running/complete tool invocations

This module is the single boundary between Astra and the bridge —
both the lean runtime tools (astra/runtime/tools/local.py) and the
HTTP routes the daemon hits (services/stream/main.py) call into the
same functions here. Auth, allowlist checks, and queue-state
transitions all live in one place.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
import os
import secrets
import time
from datetime import datetime
from typing import Any

from sqlalchemy import text

from astra.db.engine import async_session

logger = logging.getLogger(__name__)


# ── Models ────────────────────────────────────────────────


@dataclasses.dataclass
class BridgeToken:
    """An authenticated daemon credential."""

    id: int
    label: str
    allowed_paths: list[str]
    allowed_bash_patterns: list[str] | None
    created_at: datetime
    last_seen_at: datetime | None = None


@dataclasses.dataclass
class BridgeCall:
    """A queued tool invocation. Status transitions:
    pending → running → (complete | failed | timeout)."""

    id: int
    bridge_token_id: int
    tool_name: str
    args: dict[str, Any]
    result: str | None
    status: str
    error_message: str | None
    created_at: datetime
    picked_up_at: datetime | None
    completed_at: datetime | None


# ── Token operations ──────────────────────────────────────


def _hash_token(plaintext: str) -> str:
    """SHA-256 hex of the token. Plain-text never lands in the DB."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


async def issue_bridge_token(
    *,
    label: str,
    allowed_paths: list[str],
    allowed_bash_patterns: list[str] | None = None,
) -> tuple[str, int]:
    """Mint a new bridge token. Returns (plaintext_token, token_id).

    The plaintext is shown to the user ONCE — only the hash is stored.
    Caller passes the plaintext to the daemon's startup config.
    """
    plaintext = secrets.token_urlsafe(32)
    token_hash = _hash_token(plaintext)

    async with async_session() as s:
        r = await s.execute(
            text(
                """
                INSERT INTO bridge_tokens
                  (token_hash, label, allowed_paths, allowed_bash_patterns)
                VALUES
                  (:h, :l, CAST(:p AS JSONB), CAST(:b AS JSONB))
                RETURNING id
                """
            ),
            {
                "h": token_hash,
                "l": label[:127],
                "p": json.dumps(list(allowed_paths)),
                "b": json.dumps(allowed_bash_patterns) if allowed_bash_patterns else None,
            },
        )
        token_id = int(r.one()[0])
        await s.commit()

    logger.info(
        "[bridge] minted token id=%s label=%r paths=%d",
        token_id,
        label,
        len(allowed_paths),
    )
    return plaintext, token_id


async def expand_bridge_allowlist(
    token_id: int, *, add_paths: list[str]
) -> tuple[list[str], list[str]]:
    """Append new paths to a token's allowed_paths array.

    Returns (new_allowlist, paths_that_were_added).
    Paths already in the allowlist are silently skipped.
    Refuses to add paths that don't start with `/` (must be absolute).

    This is the chat-command path that lets the user expand bridge
    access mid-conversation without restarting the daemon. The
    daemon picks up the new policy on its next poll because
    validate_bridge_token re-reads allowed_paths from the row each
    time.
    """
    cleaned = [p.strip() for p in add_paths if p and p.strip()]
    cleaned = [p for p in cleaned if p.startswith("/")]
    if not cleaned:
        return [], []

    async with async_session() as s:
        r = await s.execute(
            text(
                "SELECT allowed_paths FROM bridge_tokens WHERE id = :id"
            ),
            {"id": int(token_id)},
        )
        row = r.first()
        if not row:
            return [], []
        existing = row[0] or []
        if isinstance(existing, str):
            try:
                existing = json.loads(existing)
            except json.JSONDecodeError:
                existing = []
        existing_set = {os.path.normpath(p) for p in existing}
        added: list[str] = []
        for p in cleaned:
            if os.path.normpath(p) in existing_set:
                continue
            existing.append(p)
            existing_set.add(os.path.normpath(p))
            added.append(p)
        if added:
            await s.execute(
                text(
                    """
                    UPDATE bridge_tokens
                    SET allowed_paths = CAST(:p AS JSONB)
                    WHERE id = :id
                    """
                ),
                {"id": int(token_id), "p": json.dumps(existing)},
            )
            await s.commit()
            logger.info(
                "[bridge] expanded token=%s with %d new path(s): %s",
                token_id,
                len(added),
                added,
            )
    return list(existing), added


async def revoke_bridge_token(token_id: int) -> bool:
    async with async_session() as s:
        r = await s.execute(
            text("UPDATE bridge_tokens SET revoked_at = now() WHERE id = :id AND revoked_at IS NULL"),
            {"id": int(token_id)},
        )
        await s.commit()
        return (r.rowcount or 0) > 0


async def validate_bridge_token(plaintext: str) -> BridgeToken | None:
    """Look up an active token by plaintext. Updates last_seen_at."""
    if not plaintext:
        return None
    token_hash = _hash_token(plaintext)
    async with async_session() as s:
        r = await s.execute(
            text(
                """
                SELECT id, label, allowed_paths, allowed_bash_patterns,
                       created_at, last_seen_at
                FROM bridge_tokens
                WHERE token_hash = :h AND revoked_at IS NULL
                """
            ),
            {"h": token_hash},
        )
        row = r.first()
        if not row:
            return None
        # Best-effort heartbeat update
        try:
            await s.execute(
                text("UPDATE bridge_tokens SET last_seen_at = now() WHERE id = :id"),
                {"id": int(row[0])},
            )
            await s.commit()
        except Exception:
            pass

    paths = row[2] or []
    if isinstance(paths, str):
        try:
            paths = json.loads(paths)
        except json.JSONDecodeError:
            paths = []
    bash_patterns = row[3]
    if isinstance(bash_patterns, str):
        try:
            bash_patterns = json.loads(bash_patterns)
        except json.JSONDecodeError:
            bash_patterns = None

    return BridgeToken(
        id=int(row[0]),
        label=str(row[1] or ""),
        allowed_paths=list(paths),
        allowed_bash_patterns=bash_patterns,
        created_at=row[4],
        last_seen_at=row[5],
    )


def is_path_allowed(path: str, allowed_paths: list[str]) -> bool:
    """Defensive server-side allowlist check. The daemon does its own
    check too — this is belt + suspenders."""
    if not path:
        return False
    try:
        # Resolve to absolute (no .. traversal) without touching the
        # filesystem (server doesn't have local FS, only the daemon does).
        normalized = os.path.normpath(path)
        if not normalized.startswith("/"):
            return False
    except Exception:
        return False
    for root in allowed_paths or []:
        try:
            root_normalized = os.path.normpath(root)
            # Match if the requested path equals or sits inside the root
            if normalized == root_normalized:
                return True
            if normalized.startswith(root_normalized.rstrip("/") + "/"):
                return True
        except Exception:
            continue
    return False


# ── Call queue operations ─────────────────────────────────


async def queue_call(
    *,
    bridge_token_id: int,
    tool_name: str,
    args: dict[str, Any],
) -> int:
    """Insert a pending call. Returns its id."""
    async with async_session() as s:
        r = await s.execute(
            text(
                """
                INSERT INTO bridge_calls
                  (bridge_token_id, tool_name, args, status)
                VALUES (:tid, :tn, CAST(:a AS JSONB), 'pending')
                RETURNING id
                """
            ),
            {
                "tid": int(bridge_token_id),
                "tn": tool_name[:63],
                "a": json.dumps(args),
            },
        )
        call_id = int(r.one()[0])
        await s.commit()
        return call_id


async def claim_pending_call(bridge_token_id: int) -> BridgeCall | None:
    """Atomically claim the oldest pending call for a daemon.

    Uses SELECT ... FOR UPDATE SKIP LOCKED so concurrent daemons (if
    we ever support them) don't double-claim the same row. Sets
    status='running' and picked_up_at = now() in the same statement.
    Returns None if no pending calls exist.
    """
    async with async_session() as s:
        r = await s.execute(
            text(
                """
                UPDATE bridge_calls
                SET status = 'running', picked_up_at = now()
                WHERE id = (
                  SELECT id FROM bridge_calls
                  WHERE bridge_token_id = :tid AND status = 'pending'
                  ORDER BY created_at ASC
                  FOR UPDATE SKIP LOCKED
                  LIMIT 1
                )
                RETURNING id, tool_name, args, created_at
                """
            ),
            {"tid": int(bridge_token_id)},
        )
        row = r.first()
        await s.commit()
        if not row:
            return None

    args = row[2] or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {}

    return BridgeCall(
        id=int(row[0]),
        bridge_token_id=bridge_token_id,
        tool_name=str(row[1]),
        args=dict(args),
        result=None,
        status="running",
        error_message=None,
        created_at=row[3],
        picked_up_at=None,
        completed_at=None,
    )


async def finalize_call(
    call_id: int,
    *,
    ok: bool,
    result: str = "",
    error_message: str | None = None,
) -> None:
    """Daemon reports tool execution result. Status flips to
    'complete' or 'failed'."""
    status = "complete" if ok else "failed"
    async with async_session() as s:
        await s.execute(
            text(
                """
                UPDATE bridge_calls
                SET status = :st,
                    result = :r,
                    error_message = :em,
                    completed_at = now()
                WHERE id = :id
                """
            ),
            {
                "id": int(call_id),
                "st": status,
                "r": (result or "")[:1_048_576],  # 1MB cap
                "em": (error_message or None) and error_message[:4000],
            },
        )
        await s.commit()


async def wait_for_result(
    call_id: int,
    *,
    timeout_sec: float = 30.0,
    poll_interval_sec: float = 0.25,
) -> BridgeCall:
    """Poll until the call resolves (status complete/failed) or timeout.

    Returns the final BridgeCall row. On timeout, marks the row
    status='timeout' and returns it — Astra's tool sees is_error=True
    and the model can decide how to recover.
    """
    deadline = time.monotonic() + timeout_sec
    while True:
        async with async_session() as s:
            r = await s.execute(
                text(
                    """
                    SELECT id, bridge_token_id, tool_name, args, result,
                           status, error_message, created_at, picked_up_at,
                           completed_at
                    FROM bridge_calls WHERE id = :id
                    """
                ),
                {"id": int(call_id)},
            )
            row = r.first()
        if not row:
            raise RuntimeError(f"bridge call {call_id} not found")
        status = str(row[5])
        if status in ("complete", "failed", "timeout"):
            args = row[3] or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            return BridgeCall(
                id=int(row[0]),
                bridge_token_id=int(row[1]),
                tool_name=str(row[2]),
                args=dict(args),
                result=row[4],
                status=status,
                error_message=row[6],
                created_at=row[7],
                picked_up_at=row[8],
                completed_at=row[9],
            )
        if time.monotonic() >= deadline:
            # Mark as timed out so future readers see explicit state
            try:
                async with async_session() as s:
                    await s.execute(
                        text(
                            """
                            UPDATE bridge_calls
                            SET status = 'timeout',
                                error_message = COALESCE(error_message,
                                  'astra-side timeout — daemon did not respond'),
                                completed_at = now()
                            WHERE id = :id AND status NOT IN ('complete','failed')
                            """
                        ),
                        {"id": int(call_id)},
                    )
                    await s.commit()
            except Exception:
                pass
            return BridgeCall(
                id=call_id,
                bridge_token_id=0,
                tool_name="",
                args={},
                result=None,
                status="timeout",
                error_message="astra-side timeout",
                created_at=datetime.utcnow(),
                picked_up_at=None,
                completed_at=datetime.utcnow(),
            )
        await asyncio.sleep(poll_interval_sec)
