"""
Owner-window guard — make outbound-to-Kunal survive Meta's 24h rule.

The failure this exists to kill (found 2026-07-03): Kunal last texted
Astra on Jun 22; Meta's 24h customer-service window closed on Jun 23;
every free-form send since (briefings, draft deliveries, alerts) was
ACCEPTED by the Graph API (200 + message id) and then silently dropped
— the failure statuses land on HelmTech's webhook (shared number), so
Astra never saw them and kept logging whatsapp=True for 11 days.

The guard:
  * `window_open(phone)` — is there an inbound from this owner within
    the last WINDOW_HOURS? (Ground truth from our own messages table.)
  * Window CLOSED → callers must NOT free-form send. They queue the
    text here (`queue_pending`) and we send ONE approved TEMPLATE
    (default hello_world — templates deliver outside the window) as a
    "reply to reopen" knock, deduped to once per closure.
  * The webhook calls `flush_pending(phone)` the moment an owner
    inbound arrives — the window just opened, so everything queued
    during the closure is delivered immediately.

All DB access is on isolated sessions with ensure-at-use DDL (the
voice_profile lesson: a side-read must never poison a caller's txn).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from gateway.db.engine import async_session

logger = logging.getLogger(__name__)

WINDOW_HOURS = 23.5   # margin under Meta's 24h
REOPEN_DEDUPE_HOURS = 20.0
FLUSH_MAX = 6         # newest N queued texts delivered on reopen

_ensured = False
_ENSURE = text(
    "CREATE TABLE IF NOT EXISTS pending_owner_notifications ("
    "id SERIAL PRIMARY KEY, "
    "phone TEXT NOT NULL, "
    "kind TEXT NOT NULL DEFAULT 'notify', "
    "body TEXT NOT NULL DEFAULT '', "
    "created_at TIMESTAMPTZ NOT NULL DEFAULT now(), "
    "sent_at TIMESTAMPTZ)"
)


async def _ensure() -> None:
    global _ensured
    if _ensured:
        return
    try:
        async with async_session() as s:
            await s.execute(_ENSURE)
            await s.commit()
        _ensured = True
    except Exception as e:
        logger.warning("[owner_window] ensure failed: %s", e)


async def window_open(phone: str) -> bool | None:
    """True/False from our inbound record; None if we can't tell (fail
    open — callers should attempt the free-form send on None)."""
    try:
        async with async_session() as s:
            r = await s.execute(
                text(
                    "SELECT max(m.created_at) FROM messages m "
                    "JOIN conversations c ON m.conversation_id = c.id "
                    "JOIN contacts ct ON c.contact_id = ct.id "
                    "WHERE lower(m.direction::text) = 'inbound' "
                    "AND regexp_replace(ct.phone, '\\D', '', 'g') = :p"
                ),
                {"p": "".join(ch for ch in phone if ch.isdigit())},
            )
            last_in = r.scalar()
        if last_in is None:
            return False
        if last_in.tzinfo is None:
            last_in = last_in.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - last_in) < timedelta(hours=WINDOW_HOURS)
    except Exception as e:
        logger.info("[owner_window] window check failed (fail-open): %s", e)
        return None


async def queue_pending(phone: str, body: str) -> None:
    await _ensure()
    async with async_session() as s:
        await s.execute(
            text(
                "INSERT INTO pending_owner_notifications (phone, kind, body) "
                "VALUES (:p, 'notify', :b)"
            ),
            {"p": phone, "b": body[:8000]},
        )
        await s.commit()


async def maybe_send_reopener(phone: str) -> bool:
    """Knock on the closed window with an approved template — at most
    once per REOPEN_DEDUPE_HOURS. Returns True if a knock was sent."""
    await _ensure()
    try:
        async with async_session() as s:
            r = await s.execute(
                text(
                    "SELECT max(created_at) FROM pending_owner_notifications "
                    "WHERE phone = :p AND kind = 'reopener'"
                ),
                {"p": phone},
            )
            last = r.scalar()
        if last is not None:
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - last) < timedelta(hours=REOPEN_DEDUPE_HOURS):
                return False

        from gateway.services.meta_api import MetaAPIClient

        tpl = os.environ.get("WA_REOPEN_TEMPLATE", "hello_world").strip()
        lang = os.environ.get("WA_REOPEN_TEMPLATE_LANG", "en_US").strip()
        client = MetaAPIClient()
        try:
            result = await client.send_template(
                phone=phone, template_name=tpl, language_code=lang
            )
        finally:
            await client.close()
        if not result.success:
            logger.warning("[owner_window] reopener template failed: %s", result.error)
            return False

        async with async_session() as s:
            await s.execute(
                text(
                    "INSERT INTO pending_owner_notifications "
                    "(phone, kind, body, sent_at) "
                    "VALUES (:p, 'reopener', :b, now())"
                ),
                {"p": phone, "b": f"template:{tpl}"},
            )
            await s.commit()
        logger.info("[owner_window] sent reopener template '%s' to %s", tpl, phone)
        return True
    except Exception as e:
        logger.warning("[owner_window] reopener failed: %s", e)
        return False


async def flush_pending(phone: str) -> int:
    """Owner just messaged us — the window is open. Deliver what queued
    up during the closure: the newest FLUSH_MAX texts, oldest-first, and
    mark everything older as sent (superseded) so it can't backlog-spam.
    Returns the number delivered. Best-effort; never raises."""
    try:
        await _ensure()
        async with async_session() as s:
            r = await s.execute(
                text(
                    "SELECT id, body FROM pending_owner_notifications "
                    "WHERE phone = :p AND kind = 'notify' AND sent_at IS NULL "
                    "ORDER BY created_at DESC LIMIT :n"
                ),
                {"p": phone, "n": FLUSH_MAX},
            )
            rows = list(r.all())
        if not rows:
            return 0
        rows.reverse()  # deliver oldest→newest of the kept window

        from gateway.services.meta_api import MetaAPIClient

        client = MetaAPIClient()
        delivered_ids: list[int] = []
        try:
            for row in rows:
                result = await client.send_text(phone=phone, body=row.body[:4096])
                if not result.success:
                    logger.warning(
                        "[owner_window] flush send failed at id=%s: %s",
                        row.id, result.error,
                    )
                    break
                delivered_ids.append(row.id)
        finally:
            await client.close()

        if delivered_ids:
            async with async_session() as s:
                await s.execute(
                    text(
                        "UPDATE pending_owner_notifications SET sent_at = now() "
                        "WHERE id = ANY(:ids) OR (phone = :p AND kind = 'notify' "
                        "AND sent_at IS NULL AND created_at < ("
                        "SELECT min(created_at) FROM pending_owner_notifications "
                        "WHERE id = ANY(:ids)))"
                    ),
                    {"ids": delivered_ids, "p": phone},
                )
                await s.commit()
        logger.info("[owner_window] flushed %d pending to %s", len(delivered_ids), phone)
        return len(delivered_ids)
    except Exception as e:
        logger.warning("[owner_window] flush failed: %s", e)
        return 0
