"""Capability-broker transport — the cloud half.

This module carries REQUESTS. It carries no authority, and there is
deliberately nothing in the schema for it to set: no `approved`, no
`approver`, no `token`, no `approval_pubkey`, no `permission_epoch`, no
`receipt_verified`. The brain's DB role is `postgres`, a superuser and
the only login role in the cluster, so any such column would be a
column the brain can set on itself — which is what `browser_tasks.
approved` became, and why it had to be defused by a JOIN.

What A3 claims, precisely, and no more: **a row in Postgres cannot
become an authorization, and the cloud cannot fabricate an attested
result.** It does NOT claim "no credential the cloud holds can cause a
physical action": that is false today and stays false while five of
the eight catalogue verbs are `auto` (no fingerprint) and `local_bash`
is still registered (it goes with the bridge in A6). What A5 added: no
model tool writes `approvals` any more; the resolver and its gate
exemption are gone. Say the narrow true thing.

Authority lives entirely on the Mac: the broker canonicalises the
arguments itself, renders the display itself, and mints a token with a
key the cloud has never seen; the executor verifies both signatures
against public keys compiled into its own binary. Everything below is
plumbing around that.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import secrets
from datetime import datetime
from typing import Any

from sqlalchemy import text

from astra.db.engine import async_session

logger = logging.getLogger(__name__)

__all__ = [
    "Body", "Intent",
    "mint_body_token", "validate_body_token", "touch_body_poll",
    "submit_intent", "claim_next_intent", "record_status",
    "append_audit", "get_intent_status", "expire_stale_intents",
]


# ── Models ────────────────────────────────────────────────


@dataclasses.dataclass
class Body:
    id: int
    label: str
    created_at: datetime
    last_poll_at: datetime | None
    last_completed_at: datetime | None


@dataclasses.dataclass
class Intent:
    id: int
    verb: str
    args: dict[str, Any]
    why: str
    expires_at: datetime


# ── Tokens ────────────────────────────────────────────────
#
# SHA-256 of the bearer, never the plaintext. Mirrors
# astra/runtime/bridge/store.py. Do NOT copy astra/shares/store.py,
# which stores its token in plaintext.


def _hash_token(tok: str) -> str:
    return hashlib.sha256(tok.encode("utf-8")).hexdigest()


async def mint_body_token(label: str) -> tuple[int, str]:
    """Register a body. Returns (id, plaintext) — the plaintext is
    returned ONCE and never stored."""
    tok = secrets.token_urlsafe(32)
    async with async_session() as s:
        row = (await s.execute(
            text("""
                INSERT INTO bodies (label, token_hash)
                VALUES (:label, :h)
                RETURNING id
            """),
            {"label": label[:128], "h": _hash_token(tok)},
        )).first()
        await s.commit()
    return int(row[0]), tok


async def validate_body_token(tok: str) -> Body | None:
    """Fail closed: no row, no match, None. A revoked body is None too."""
    if not tok:
        return None
    async with async_session() as s:
        row = (await s.execute(
            text("""
                SELECT id, label, created_at, last_poll_at, last_completed_at
                FROM bodies
                WHERE token_hash = :h AND revoked_at IS NULL
            """),
            {"h": _hash_token(tok)},
        )).first()
    if row is None:
        return None
    return Body(id=int(row[0]), label=row[1], created_at=row[2],
                last_poll_at=row[3], last_completed_at=row[4])


async def touch_body_poll(body_id: int) -> None:
    """Record that the body asked for work.

    Counts REQUESTS SERVED, not seconds elapsed, and is called once per
    request rather than once per loop iteration. `bridge_tokens.
    last_seen_at` counts polls, which is why it read healthy while the
    bridge served zero calls for months. Liveness must measure the thing
    you actually care about; see `last_completed_at` for that.
    """
    async with async_session() as s:
        await s.execute(
            text("UPDATE bodies SET last_poll_at = now() WHERE id = :id"),
            {"id": body_id},
        )
        await s.commit()


# ── Submission (the model's only physical verb) ───────────


class ArgsRejected(ValueError):
    """The arguments cannot be stored, so the intent is not filed.

    Refused with a message the model can read and act on, never
    truncated and never silently altered — a truncated argument is an
    argument whose rendered display no longer matches what executes.
    """


def _reject_unstorable(args: dict[str, Any]) -> None:
    """Refuse what Postgres JSONB cannot hold, BEFORE the INSERT.

    A U+0000 anywhere in the document makes asyncpg raise
    UntranslatableCharacterError — 'unsupported Unicode escape
    sequence'. Verified against production. Without this check that is a
    500 on the first real request, which is exactly the failure mode
    that took the bridge result path down earlier today.
    """
    def walk(v: Any, path: str) -> None:
        if isinstance(v, str):
            if "\x00" in v:
                raise ArgsRejected(
                    f"argument {path} contains a NUL character (U+0000), "
                    "which Postgres JSONB cannot store. Remove it and "
                    "resubmit; nothing was filed."
                )
        elif isinstance(v, dict):
            for k, sub in v.items():
                if "\x00" in str(k):
                    raise ArgsRejected(
                        f"argument key at {path} contains a NUL character"
                    )
                walk(sub, f"{path}.{k}")
        elif isinstance(v, list):
            for i, sub in enumerate(v):
                walk(sub, f"{path}[{i}]")

    walk(args, "args")


async def submit_intent(
    *, body_id: int, verb: str, args: dict[str, Any], why: str,
    ttl_seconds: int, session_claim: str = "",
) -> int:
    """File a request. Causes nothing.

    `why` is a TOP-LEVEL column, never a key inside args: `reason` is on
    the broker's forbidden-key list, so an intent carrying it inside
    args is refused wholesale by canonicalisation.
    """
    _reject_unstorable(args)
    payload = json.dumps(args, ensure_ascii=False)
    async with async_session() as s:
        row = (await s.execute(
            text("""
                INSERT INTO intents
                    (body_id, verb, args_raw, why, status,
                     session_claim, expires_at)
                VALUES
                    (:body_id, :verb, CAST(:args AS JSONB), :why, 'pending',
                     :claim, now() + make_interval(secs => :ttl))
                RETURNING id
            """),
            {"body_id": body_id, "verb": verb[:64], "args": payload,
             "why": why, "claim": session_claim[:64],
             "ttl": float(ttl_seconds)},
        )).first()
        await s.commit()
    return int(row[0])


async def pending_depth(body_id: int) -> int:
    """Queue depth, for the submission cap. A queue deeper than a human
    could plausibly approve is a flood, not a backlog."""
    async with async_session() as s:
        return int((await s.execute(
            text("""
                SELECT count(*) FROM intents
                WHERE body_id = :id AND status = 'pending'
                  AND expires_at > now()
            """),
            {"id": body_id},
        )).scalar_one())


# ── The body's side ───────────────────────────────────────


async def claim_next_intent(body_id: int) -> Intent | None:
    """Claim the oldest pending intent for this body.

    ONE statement. `FOR UPDATE SKIP LOCKED` is what makes a second body
    safe (Workstream G) — do not decompose into SELECT-then-UPDATE.
    `expires_at > now()` means an expired intent is never handed out
    even if the reaper has not run.
    """
    async with async_session() as s:
        row = (await s.execute(
            text("""
                UPDATE intents
                SET status = 'claimed', claimed_at = now()
                WHERE id = (
                    SELECT id FROM intents
                    WHERE body_id = :body_id
                      AND status = 'pending'
                      AND expires_at > now()
                    ORDER BY created_at ASC
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                RETURNING id, verb, args_raw, why, expires_at
            """),
            {"body_id": body_id},
        )).first()
        await s.commit()
    if row is None:
        return None
    args = row[2]
    if isinstance(args, str):
        args = json.loads(args)
    return Intent(id=int(row[0]), verb=row[1], args=args or {},
                  why=row[3], expires_at=row[4])


_TERMINAL = {"succeeded", "failed", "denied", "expired"}


async def record_status(
    intent_id: int, *, body_id: int, status: str,
    deny_reason: str | None = None,
    display_bytes: bytes | None = None,
    token_bytes: bytes | None = None,
    receipt_bytes: bytes | None = None,
    result_bytes: bytes | None = None,
    result_note: str = "",
) -> bool:
    """Record the body's outcome. Returns True if a row was updated.

    `body_id` is a REQUIRED keyword and the predicate is
    UNCONDITIONAL. Do not mirror
    `finalize_call(bridge_token_id: int | None = None)` — that
    optionality is backward-compatibility debt, and its own docstring
    says so. Optional means the first forgetful caller reopens
    cross-body result forgery, and with one body registered nothing
    ever fails visibly.

    The `status IN ('claimed','awaiting_human','running')` guard makes a
    terminal row unwritable, so a replayed status POST cannot rewrite a
    finished intent.

    Returning False must become a 404 at the route. Silently returning
    ok:true on a mismatch was half of the original bridge bug.
    """
    async with async_session() as s:
        r = await s.execute(
            text("""
                UPDATE intents
                SET status        = :status,
                    deny_reason   = :deny_reason,
                    display_bytes = COALESCE(:display, display_bytes),
                    token_bytes   = COALESCE(:token, token_bytes),
                    receipt_bytes = COALESCE(:receipt, receipt_bytes),
                    result_bytes  = COALESCE(:result, result_bytes),
                    result_note   = :result_note,
                    resolved_at   = CASE WHEN :terminal
                                         THEN now() ELSE resolved_at END
                WHERE id = :id
                  AND body_id = :body_id
                  AND status IN ('claimed', 'awaiting_human', 'running')
            """),
            {
                "id": int(intent_id), "body_id": int(body_id),
                "status": status, "deny_reason": deny_reason,
                "display": display_bytes, "token": token_bytes,
                "receipt": receipt_bytes, "result": result_bytes,
                "result_note": result_note[:4000],
                "terminal": status in _TERMINAL,
            },
        )
        updated = (r.rowcount or 0) > 0
        if updated and status in _TERMINAL:
            await s.execute(
                text("UPDATE bodies SET last_completed_at = now() "
                     "WHERE id = :id"),
                {"id": int(body_id)},
            )
        await s.commit()
    if not updated:
        logger.warning(
            "[broker] record_status rejected: intent %s not open for "
            "body %s (wrong body, already terminal, or absent)",
            intent_id, body_id,
        )
    return updated


async def append_audit(
    *, body_id: int, seq: int, ts: datetime, decision: str,
    prev_hash: str, record_hash: str,
    intent_id: int | None = None, verb: str = "", reason: str = "",
    args_sha256: str | None = None, display_sha256: str | None = None,
    receipt_sha256: str | None = None, actor_claimed: str = "",
) -> bool:
    """Mirror one link of the broker's audit chain. True if inserted.

    `body_id` comes from the TOKEN, never from the message body.

    ON CONFLICT DO NOTHING + RETURNING means a replayed `seq` inserts
    nothing and returns False — a body retrying after a network failure
    must not look like an attack, while overwriting seq 1 with different
    bytes must be impossible.

    This table is APPEND-ONLY and is never pruned by retention_sweep. A
    gap in `seq` is DEFINED to mean tampering; pruning it would make the
    chain verifier cry wolf nightly, and a monitor that cries wolf gets
    ignored.
    """
    async with async_session() as s:
        row = (await s.execute(
            text("""
                INSERT INTO intent_events
                    (body_id, seq, ts, intent_id, verb, decision, reason,
                     args_sha256, display_sha256, receipt_sha256,
                     actor_claimed, prev_hash, record_hash)
                VALUES
                    (:body_id, :seq, :ts, :intent_id, :verb, :decision,
                     :reason, :args_sha256, :display_sha256,
                     :receipt_sha256, :actor_claimed, :prev_hash,
                     :record_hash)
                ON CONFLICT (body_id, seq) DO NOTHING
                RETURNING id
            """),
            {
                "body_id": int(body_id), "seq": int(seq), "ts": ts,
                "intent_id": intent_id, "verb": verb[:64],
                "decision": decision[:24], "reason": reason,
                "args_sha256": args_sha256,
                "display_sha256": display_sha256,
                "receipt_sha256": receipt_sha256,
                "actor_claimed": actor_claimed[:64],
                "prev_hash": prev_hash, "record_hash": record_hash,
            },
        )).first()
        await s.commit()
    return row is not None


# ── The model's read side ─────────────────────────────────


async def get_intent_status(intent_id: int) -> dict[str, Any] | None:
    """Raw row for poll_status.

    Returns the RAW BYTES — display_bytes, receipt_bytes, result_bytes —
    not a verdict. The caller must RECOMPUTE verification from the
    executor's public key. Never add a `receipt_verified` column: that
    would be a boolean the superuser writes, presented as though the
    executor had said it.
    """
    async with async_session() as s:
        row = (await s.execute(
            text("""
                SELECT id, verb, status, deny_reason, why,
                       display_bytes, receipt_bytes, result_bytes,
                       result_note, created_at, resolved_at, expires_at
                FROM intents
                WHERE id = :id
            """),
            {"id": int(intent_id)},
        )).first()
    if row is None:
        return None
    return {
        "id": int(row[0]), "verb": row[1], "status": row[2],
        "deny_reason": row[3], "why": row[4],
        "display_bytes": row[5], "receipt_bytes": row[6],
        "result_bytes": row[7], "result_note": row[8],
        "created_at": row[9], "resolved_at": row[10],
        "expires_at": row[11],
    }


async def expire_stale_intents() -> int:
    """Reap intents whose deadline passed. Returns the count.

    Without this, `claimed` is indistinguishable from `the broker died`
    and an intent sits forever. CHARTER §8 says nothing may silently
    no-op — and the component that would notice is, in this failure,
    the absent one. So the CLOUD reaps, because the cloud is always up.
    """
    async with async_session() as s:
        r = await s.execute(
            text("""
                UPDATE intents
                SET status = 'expired', resolved_at = now(),
                    deny_reason = COALESCE(deny_reason,
                        'expired: the body did not complete this intent '
                        'before its deadline')
                WHERE expires_at <= now()
                  AND status IN ('pending', 'claimed', 'awaiting_human',
                                 'running')
            """),
        )
        n = r.rowcount or 0
        await s.commit()
    if n:
        logger.info("[broker] expired %d stale intent(s)", n)
    return n
