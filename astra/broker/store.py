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
the eight catalogue verbs are `auto` (no fingerprint). What A5 added:
no model tool writes `approvals` any more; the resolver and its gate
exemption are gone. What A6 added: the bridge and every `local_*` tool
are gone, so this pipe is the only way anything in the cloud reaches
the Mac. Say the narrow true thing.

Authority lives entirely on the Mac: the broker canonicalises the
arguments itself, renders the display itself, and mints a token with a
key the cloud has never seen; the executor verifies both signatures
against public keys compiled into its own binary. Everything below is
plumbing around that.

The session factory is looked up at each use (`_engine.async_session()`),
never bound at import. tests/conftest.py guards against the production
database by rebinding `astra.db.engine.async_session` for the test
session; a module that copied the factory at import time kept the
original and walked straight past the guard. Every function here must
reach the factory through the module attribute.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from astra.db import engine as _engine

logger = logging.getLogger(__name__)

__all__ = [
    "Body", "BodyLiveness", "Intent",
    "mint_body_token", "validate_body_token", "touch_body_poll",
    "body_liveness", "open_intents",
    "submit_intent", "claim_next_intent", "record_status",
    "append_audit", "get_intent_status", "wait_for_intent",
    "list_resolved_since", "list_resolved_after_turn_end",
    "expire_stale_intents",
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
class BodyLiveness:
    """What the cloud can honestly say about a body's presence.

    `poll_age_sec` is measured on the DATABASE clock (now() minus
    last_poll_at in the same statement), so a skewed container clock
    cannot make a live body look dead or a dead one look live. None
    means the body has never polled.
    """
    body_id: int
    label: str
    last_poll_at: datetime | None
    last_completed_at: datetime | None
    poll_age_sec: float | None
    revoked: bool


@dataclasses.dataclass
class Intent:
    id: int
    verb: str
    args: dict[str, Any]
    why: str
    expires_at: datetime


# ── Tokens ────────────────────────────────────────────────
#
# SHA-256 of the bearer, never the plaintext. Same discipline the
# retired bridge store had (astra/runtime/bridge/store.py, deleted in
# Phase A6). Do NOT copy astra/shares/store.py, which stores its token
# in plaintext.


def _hash_token(tok: str) -> str:
    return hashlib.sha256(tok.encode("utf-8")).hexdigest()


async def mint_body_token(label: str) -> tuple[int, str]:
    """Register a body. Returns (id, plaintext) — the plaintext is
    returned ONCE and never stored."""
    tok = secrets.token_urlsafe(32)
    async with _engine.async_session() as s:
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
    async with _engine.async_session() as s:
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
    request rather than once per loop iteration. The retired bridge's
    `bridge_tokens.last_seen_at` counted polls, which is why it read
    healthy while the bridge served zero calls for months. Liveness must measure the thing
    you actually care about; see `last_completed_at` for that.
    """
    async with _engine.async_session() as s:
        await s.execute(
            text("UPDATE bodies SET last_poll_at = now() WHERE id = :id"),
            {"id": body_id},
        )
        await s.commit()


async def body_liveness(body_id: int) -> BodyLiveness | None:
    """Presence of one body, for the fast-refusal in the intent client.

    Read-only. `last_poll_at` says the broker asked for work;
    `last_completed_at` says it finished something. The client refuses
    to file when the poll is older than its window instead of queueing
    an intent that will sit until its deadline (the laptop is closed:
    normal, and not worth a 300 s wait to discover).
    """
    async with _engine.async_session() as s:
        row = (await s.execute(
            text("""
                SELECT id, label, last_poll_at, last_completed_at,
                       EXTRACT(EPOCH FROM (now() - last_poll_at)),
                       revoked_at IS NOT NULL
                FROM bodies
                WHERE id = :id
            """),
            {"id": int(body_id)},
        )).first()
    if row is None:
        return None
    return BodyLiveness(
        body_id=int(row[0]), label=row[1], last_poll_at=row[2],
        last_completed_at=row[3],
        poll_age_sec=None if row[4] is None else float(row[4]),
        revoked=bool(row[5]),
    )


async def sole_live_body() -> BodyLiveness | None:
    """The one enrolled body, if there is exactly one and it is polling.

    For scheduled checks that have no body_id to hand. Returns None when
    there is no body, more than one, or the body is not polling — all
    three of which are reasons to do nothing quietly rather than guess.
    A Mac that is not polling is a closed laptop, which is normal.
    """
    async with _engine.async_session() as s:
        rows = (await s.execute(text(
            "SELECT id FROM bodies WHERE revoked_at IS NULL ORDER BY id"
        ))).all()
    if len(rows) != 1:
        return None
    live = await body_liveness(int(rows[0][0]))
    if live is None or live.poll_age_sec is None:
        return None
    # 60 s, the same window client.BODY_POLL_WINDOW_SEC uses. Not
    # imported: client imports THIS module, and a cycle here would be
    # paid at every import. tests/test_broker/test_client_refusals.py
    # asserts the two stay equal.
    return live if live.poll_age_sec <= 60 else None


async def last_successful_probes(*, within_days: int = 7) -> dict[str, bool]:
    """Which body.probe targets have answered "yes" recently.

    The probe results ARE the history, so the previous state is read
    back out of `intents` rather than kept in memory (lost on every
    scheduler restart) or in a new table (a second fact to keep in
    sync). A target absent from this map has never succeeded in the
    window, which is NOT a regression — it is a grant nobody has made.

    Read-only.
    """
    async with _engine.async_session() as s:
        rows = (await s.execute(
            text("""
                SELECT args_raw ->> 'target' AS target
                FROM intents
                WHERE verb = 'body.probe'
                  AND status = 'succeeded'
                  AND created_at > now() - make_interval(days => :d)
                  AND encode(result_bytes, 'escape') LIKE 'opened: yes%'
                GROUP BY 1
            """),
            {"d": int(within_days)},
        )).all()
    return {r[0]: True for r in rows if r[0]}


async def open_intents(body_id: int, *, limit: int = 5) -> list[dict[str, Any]]:
    """Intents the broker has TAKEN and not finished: claimed,
    awaiting Kunal's fingerprint, or running, with a deadline still
    ahead. Oldest first. Read-only.

    Why the intent client needs this beside `body_liveness`: the
    broker's serve loop is single-threaded (ServeLoop.runForever ->
    runOnce -> handle), so while it is inside handle() it does not
    call /broker/intents/next and `last_poll_at` stops moving. A
    signed verb with the approver connected blocks there for up to
    approvalTimeoutSec (3600 s) with the Touch ID prompt on screen; an
    auto verb whose execution runs past the poll window does the same.
    Judged by poll age alone that body looks closed. A row here says
    the opposite: the Mac is up and busy, and the client must say
    "busy with #N", never "laptop closed".

    'pending' is deliberately excluded: a pending row means nobody has
    claimed it, which says nothing about the broker being alive.
    `expires_at > now()` bounds a dead broker's shadow to the intent's
    own TTL (300 s auto, 3600 s signed); the reaper closes it after
    that and the body reads as offline again.
    """
    async with _engine.async_session() as s:
        rows = (await s.execute(
            text("""
                SELECT id, verb, status, claimed_at, expires_at,
                       EXTRACT(EPOCH FROM (now() - claimed_at))
                FROM intents
                WHERE body_id = :id
                  AND status IN ('claimed', 'awaiting_human', 'running')
                  AND expires_at > now()
                ORDER BY claimed_at ASC NULLS LAST, id ASC
                LIMIT :limit
            """),
            {"id": int(body_id), "limit": int(limit)},
        )).fetchall()
    return [
        {
            "id": int(r[0]), "verb": r[1], "status": r[2],
            "claimed_at": r[3], "expires_at": r[4],
            "claimed_age_sec": None if r[5] is None else float(r[5]),
        }
        for r in rows
    ]


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
    async with _engine.async_session() as s:
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
    async with _engine.async_session() as s:
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
    async with _engine.async_session() as s:
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
    async with _engine.async_session() as s:
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
    async with _engine.async_session() as s:
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


# One column list, shared by every status read, so a row from
# wait_for_intent or list_resolved_since is the same shape the
# receipt verifier consumes.
_STATUS_COLUMNS = """
    id, verb, status, deny_reason, why,
    display_bytes, receipt_bytes, result_bytes,
    result_note, created_at, resolved_at, expires_at,
    session_claim
"""


def _status_row(row: Any) -> dict[str, Any]:
    return {
        "id": int(row[0]), "verb": row[1], "status": row[2],
        "deny_reason": row[3], "why": row[4],
        "display_bytes": row[5], "receipt_bytes": row[6],
        "result_bytes": row[7], "result_note": row[8],
        "created_at": row[9], "resolved_at": row[10],
        "expires_at": row[11], "session_claim": row[12],
    }


async def get_intent_status(intent_id: int) -> dict[str, Any] | None:
    """Raw row for poll_status.

    Returns the RAW BYTES — display_bytes, receipt_bytes, result_bytes —
    not a verdict. The caller must RECOMPUTE verification from the
    executor's public key. Never add a `receipt_verified` column: that
    would be a boolean the superuser writes, presented as though the
    executor had said it.
    """
    async with _engine.async_session() as s:
        row = (await s.execute(
            text(f"SELECT {_STATUS_COLUMNS} FROM intents WHERE id = :id"),
            {"id": int(intent_id)},
        )).first()
    if row is None:
        return None
    return _status_row(row)


async def wait_for_intent(
    intent_id: int, *, timeout_sec: float, poll_interval_sec: float = 0.5,
) -> dict[str, Any] | None:
    """Watch one intent until it is terminal, or give up.

    Returns the raw status row (same shape as get_intent_status) once
    the status is terminal, and None when `timeout_sec` passes first
    or no such row exists. It reads and only reads: the row is never
    written by the watcher. The reaper (expire_stale_intents) owns
    expiry, and the broker may still finish an intent after the cloud
    stopped watching it; the old bridge's wait_for_result marked rows
    'timeout' on its own patience, which made the courier's deadline
    part of the row's truth. None here means "not finished yet as far
    as this caller waited", nothing more.
    """
    deadline = time.monotonic() + float(timeout_sec)
    while True:
        row = await get_intent_status(intent_id)
        if row is None:
            return None
        if row["status"] in _TERMINAL:
            return row
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        await asyncio.sleep(min(float(poll_interval_sec), remaining))


async def list_resolved_since(
    ts: datetime, *, session_prefix: str, limit: int = 50,
) -> list[dict[str, Any]]:
    """Terminal intents that resolved after `ts` whose session_claim
    starts with `session_prefix` ("turn:" for chat-filed intents,
    "job:" for scheduler jobs), oldest first.

    For the completion notices and the "intents resolved since your
    last turn" block: a signed verb finishes after the turn that filed
    it has ended, and nothing else tells the next turn. Read-only, and
    it keeps no delivery mark of its own: the caller tracks its own
    high-water timestamp, so two readers (the push job and the prompt
    builder) never race over one flag.
    """
    async with _engine.async_session() as s:
        rows = (await s.execute(
            text(f"""
                SELECT {_STATUS_COLUMNS} FROM intents
                WHERE resolved_at > :ts
                  AND status IN ('succeeded', 'failed', 'denied', 'expired')
                  AND starts_with(session_claim, :prefix)
                ORDER BY resolved_at ASC
                LIMIT :limit
            """),
            {"ts": ts, "prefix": session_prefix, "limit": int(limit)},
        )).fetchall()
    return [_status_row(r) for r in rows]


# A turn row whose `ended_at` is still NULL this long after it started
# belongs to a process that died mid-turn (the runner's hard cap is
# 240 s; finalize_turn_record swallows its own errors). Its intents
# would otherwise never be reported to anyone.
DEAD_TURN_AFTER_SEC = 600


async def list_resolved_after_turn_end(
    ts: datetime, *, since_id: int = 0, limit: int = 50,
    newest_first: bool = False,
) -> list[dict[str, Any]]:
    """Terminal chat-filed intents that resolved AFTER the turn that
    filed them had ended, in (resolved_at, id) order past `(ts,
    since_id)`.

    This is the one query behind both the completion push
    (jobs.broker_notify) and the "intents resolved since your last
    turn" prompt block (context/now.py), so the two can never disagree
    about what "resolved after the turn" means. The JOIN is on
    session_claim = 'turn:' || turns.id, which is what run_lean_turn
    writes; a CLI turn claims 'turn:<session id>', matches no row and
    is never reported (it waited synchronously). An auto verb that
    finished inside its turn was consumed there through poll_status
    or run_intent(wait_sec=...) and is deliberately excluded: the
    first version pushed one notice per fs.read page of a chat
    ingest, four pushes for reads the turn had already read, which
    trains the channel that will later carry signed-verb outcomes to
    be dismissed. A turn with no ended_at that started more than
    DEAD_TURN_AFTER_SEC ago counts as ended (the cutoff is bound as a
    parameter, so the same statement runs on sqlite in
    tests/test_scheduler/test_broker_notify.py against fixture rows).
    Read-only; the caller keeps its own high-water mark.
    """
    order = "DESC" if newest_first else "ASC"
    dead_before = datetime.now(timezone.utc) - timedelta(seconds=DEAD_TURN_AFTER_SEC)
    async with _engine.async_session() as s:
        rows = (await s.execute(
            text(f"""
                SELECT {", ".join("i." + c.strip() for c in _STATUS_COLUMNS.split(","))}
                FROM intents i
                JOIN turns t ON i.session_claim = 'turn:' || CAST(t.id AS TEXT)
                WHERE i.resolved_at IS NOT NULL
                  AND i.status IN ('succeeded', 'failed', 'denied', 'expired')
                  AND (i.resolved_at, i.id) > (:ts, :since_id)
                  AND (
                        (t.ended_at IS NOT NULL AND i.resolved_at > t.ended_at)
                     OR (t.ended_at IS NULL AND t.started_at < :dead_before)
                  )
                ORDER BY i.resolved_at {order}, i.id {order}
                LIMIT :limit
            """),
            {"ts": ts, "since_id": int(since_id), "limit": int(limit),
             "dead_before": dead_before},
        )).fetchall()
    return [_status_row(r) for r in rows]


async def expire_stale_intents() -> int:
    """Reap intents whose deadline passed. Returns the count.

    Without this, `claimed` is indistinguishable from `the broker died`
    and an intent sits forever. CHARTER §8 says nothing may silently
    no-op — and the component that would notice is, in this failure,
    the absent one. So the CLOUD reaps, because the cloud is always up.
    """
    async with _engine.async_session() as s:
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
