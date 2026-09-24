"""Scheduled, read-only R2 verification. Never calls the destructive probe.

These DB checkpoints and pushes are operational evidence, not authority:
the cloud still owns its DB. The trust anchor is the out-of-band chain pin
and the signed objects. A compromised cloud can suppress this monitor;
the independent phone/reader remains part of the acceptance ceremony.

Full walk daily and at startup; signed-head freshness every ten minutes.
Only the explicitly paired body's activity can turn stale shipping into
an alert. Closed-lid freshness is deferred, not verified and not paged.
Successful notification state survives restarts. Delivery is at-least-once
(a crash between push and checkpoint can repeat one), never marked sent
when the push service delivered to zero subscriptions.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from sqlalchemy import text

from astra.broker import audit_anchor as anchor
from astra.broker import store
from astra.db import engine as db

logger = logging.getLogger(__name__)
KINDS = ("verify", "freshness")
MAX_AGE = {"verify": 26 * 3600, "freshness": 20 * 60}
HEALTH_DB_TIMEOUT = 5


@dataclass(frozen=True)
class Observation:
    check_kind: str
    chain_id: str
    checked_at: datetime
    state: str
    verdict: str
    record_count: int = 0
    head_seq: int | None = None

    @property
    def alert_key(self) -> str | None:
        if self.state == "deferred":
            return None
        return "healthy" if self.state == "ok" else f"{self.state}:{self.verdict}"


def _read(kind: str, cfg: anchor.Config) -> anchor.Result:
    """Blocking SDK stays off the scheduler loop; reads are budgeted.

    Deadline is checked between requests; an in-flight SDK request is bounded
    by its connect/read timeout and retry budget. No abandoned worker threads.
    """
    s3 = anchor.client(cfg)
    try:
        reader = anchor.Anchor(cfg, s3, deadline=time.monotonic() + 300)
        return anchor.verify(reader) if kind == "verify" else anchor.freshness(reader)
    finally:
        s3.close()


def classify(kind: str, chain_id: str, result: anchor.Result,
             *, online: bool | None, now: datetime) -> Observation:
    # Nothing from an object, its filename, or an exception belongs in a
    # notification/DB/health response. Only a bounded diagnostic code.
    code = result.verdict
    if not re.fullmatch(r"[a-z_]{1,48}", code):
        code = "invalid_verifier_result"
    state = "ok" if result.ok and code == {"verify": "intact", "freshness": "fresh"}[kind] else "failed"
    if kind == "freshness" and code in ("stale", "no_head"):
        if online is False:
            state = "deferred"
        elif online is None:
            code = "body_activity_unknown"
    return Observation(kind, chain_id, now, state, code,
                       max(0, result.records), result.head_seq)


async def observe(kind: str) -> Observation:
    if kind not in KINDS:
        raise ValueError("unknown audit check")
    now = datetime.now(timezone.utc)
    chain_id = ""
    try:
        cfg = anchor.Config.from_env()
        if not re.fullmatch(r"[0-9a-f]{64}", cfg.chain_id):
            raise ValueError("chain pin invalid")
        chain_id = cfg.chain_id
        body_id = int(os.environ.get("AUDIT_BODY_ID", ""))
        if body_id < 1:
            raise ValueError("body id invalid")
    except (anchor.AnchorError, ValueError):
        return Observation(kind, chain_id, now, "unconfigured", "configuration_missing_or_invalid")

    online = None
    if kind == "freshness":
        try:
            async with asyncio.timeout(5):
                body = await store.body_liveness(body_id)
            if body is not None and not body.revoked:
                online = body.poll_age_sec is not None and 0 <= body.poll_age_sec <= 180
        except Exception:
            # Verification can still find real tampering while the DB's
            # liveness query is unavailable. Unknown is never asleep.
            pass
    try:
        result = await asyncio.to_thread(_read, kind, cfg)
    except Exception as e:
        # SDK exceptions can contain URLs, headers and remote data.
        logger.warning("audit %s read incomplete (%s)", kind, type(e).__name__)
        return Observation(kind, chain_id, datetime.now(timezone.utc), "failed", "read_unavailable")
    return classify(kind, chain_id, result, online=online, now=datetime.now(timezone.utc))


async def save(observation: Observation) -> dict | None:
    params = asdict(observation) | {"alert_key": observation.alert_key}
    async with db.async_session() as session:
        row = (await session.execute(text("""
            INSERT INTO audit_verifications
              (check_kind, chain_id, checked_at, state, verdict, record_count, head_seq, alert_key)
            VALUES (:check_kind, :chain_id, :checked_at, :state, :verdict,
                    :record_count, :head_seq, :alert_key)
            ON CONFLICT (check_kind) DO UPDATE SET
                chain_id = excluded.chain_id, checked_at = excluded.checked_at,
                state = excluded.state, verdict = excluded.verdict,
                record_count = excluded.record_count, head_seq = excluded.head_seq,
                alert_key = excluded.alert_key,
                notified_key = CASE
                    WHEN audit_verifications.chain_id = excluded.chain_id
                    THEN audit_verifications.notified_key ELSE NULL END
            WHERE excluded.checked_at >= audit_verifications.checked_at
            RETURNING notified_key
        """), params)).mappings().first()
        await session.commit()
    return None if row is None else dict(row)


async def mark_notified(observation: Observation) -> None:
    async with db.async_session() as session:
        await session.execute(text("""
            UPDATE audit_verifications SET notified_key = :alert_key
            WHERE check_kind = :check_kind AND chain_id = :chain_id
              AND checked_at = :checked_at AND alert_key = :alert_key
        """), {"check_kind": observation.check_kind, "chain_id": observation.chain_id,
               "checked_at": observation.checked_at, "alert_key": observation.alert_key})
        await session.commit()


def notification(observation: Observation, previously_notified: str | None) -> str | None:
    target = observation.alert_key
    if target is None or target == previously_notified:
        return None
    if target == "healthy":
        return None if previously_notified is None else "Audit check recovered and verified again."
    if observation.state == "unconfigured":
        return "Audit monitoring needs its separate read-only credential, chain pin and body ID."
    if observation.verdict == "read_unavailable":
        return "Audit verification could not finish. Check reader connectivity; integrity is unverified."
    if observation.verdict in ("stale", "no_head"):
        return "The Mac is polling, but its audit anchor is missing or stale. Check the audit shipper."
    return f"Audit {observation.check_kind} check failed ({observation.verdict}). Inspect the independent verifier."


async def run_check(kind: str) -> dict:
    """Persist BEFORE notifying; DB/network failures never become a pass."""
    try:
        observation = await observe(kind)
        async with asyncio.timeout(10):
            previous = await save(observation)
        if previous is None:
            return {"status": "superseded"}
        message = notification(observation, previous["notified_key"])
        if message is not None:
            from astra.push.sender import broadcast

            try:
                delivered = await broadcast(title="astra · audit", body=message,
                                            url="/", tag=f"audit-{kind}")
                if delivered.delivered > 0:
                    async with asyncio.timeout(10):
                        await mark_notified(observation)
            except Exception as e:
                logger.warning("audit notification not confirmed (%s)", type(e).__name__)
        return {"status": observation.state, "verdict": observation.verdict}
    except Exception as e:
        logger.error("audit monitor unavailable (%s)", type(e).__name__)
        return {"status": "failed", "verdict": "monitor_unavailable"}


async def run_audit_verify() -> dict:
    return await run_check("verify")


async def run_audit_freshness() -> dict:
    return await run_check("freshness")


async def snapshots() -> list[dict]:
    async with db.async_session() as session:
        result = await session.execute(text("""
            SELECT check_kind, chain_id, checked_at, state, verdict FROM audit_verifications
        """))
        return [dict(row) for row in result.mappings().all()]


def health_report(rows: list[dict], *, now: datetime, chain_id: str) -> dict:
    checks = {}
    for kind in KINDS:
        row = next((r for r in rows if r["check_kind"] == kind), None)
        state, verdict = "failed", "not_checked"
        if row is not None:
            age = (now - row["checked_at"]).total_seconds()
            if not re.fullmatch(r"[0-9a-f]{64}", chain_id) or row["chain_id"] != chain_id:
                verdict = "chain_pin_changed_or_missing"
            elif not 0 <= age <= MAX_AGE[kind]:
                verdict = "check_overdue"
            else:
                state, verdict = row["state"], row["verdict"]
                # The public endpoint never reflects arbitrary DB strings.
                if state not in ("ok", "failed", "deferred", "unconfigured") or not re.fullmatch(r"[a-z_]{1,48}", verdict):
                    state, verdict = "failed", "invalid_checkpoint"
        checks[kind] = {"state": state, "verdict": verdict}
    states = {c["state"] for c in checks.values()}
    status = "ok" if states == {"ok"} else "deferred" if states <= {"ok", "deferred"} else "failed"
    return {"status": status, "checks": checks}


async def health() -> dict:
    try:
        async with asyncio.timeout(HEALTH_DB_TIMEOUT):
            rows = await snapshots()
        return health_report(rows, now=datetime.now(timezone.utc),
                             chain_id=os.environ.get("AUDIT_CHAIN_ID", "").strip().lower())
    except Exception:
        return {"status": "failed", "reason": "monitor_unavailable"}
