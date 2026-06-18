"""
Web Push sender — VAPID-signed notification to every active browser.

Why a thin custom wrapper around pywebpush:
  * We want a single `broadcast(title, body, url, tag)` call at the
    notification sites (briefings, catchup, meeting ready, etc).
  * We need to prune endpoints that Apple/Google report as gone (404
    or 410 response) so the table doesn't grow forever.
  * We need per-subscription failure tracking so a flaky Firebase
    endpoint doesn't block sends to the iPhone.

Thread / async story:
  pywebpush.webpush is synchronous and does a blocking HTTP POST to the
  push gateway. We wrap it in asyncio.to_thread so the scheduler's event
  loop doesn't stall. 20-30 subscriptions complete in <1s end-to-end.

Payload shape (what the service worker receives):
  {
    "title":    string,
    "body":     string,
    "url":      string,   // deep link the notification opens
    "tag":      string,   // collapses repeat notifications
    "icon":     string,   // optional, falls back to favicon
  }
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from astra.db.engine import async_session

logger = logging.getLogger(__name__)


# Apple's APNs gateway returns these when a subscription is permanently
# dead (user revoked perms, reinstalled Safari, etc). Anything else is
# treated as a transient failure and retried on the next broadcast.
DEAD_STATUS_CODES = {404, 410}


@dataclass
class BroadcastResult:
    attempted: int
    delivered: int
    pruned: int
    failed: int


async def broadcast(
    *,
    title: str,
    body: str,
    url: str = "/",
    tag: str | None = None,
    icon: str | None = None,
    include_inactive: bool = False,
) -> BroadcastResult:
    """Send a notification to every active push subscription.

    `tag` collapses repeat notifications with the same tag in the
    browser's notification tray, which is exactly what we want for
    things like "inbox preview" — we'd rather the 12:45 preview
    replace yesterday's unread one than stack up.
    """
    rows = await _fetch_subscriptions(include_inactive=include_inactive)
    if not rows:
        return BroadcastResult(0, 0, 0, 0)

    payload = json.dumps(
        {
            "title": title,
            "body": body,
            "url": url,
            "tag": tag or "astra-generic",
            "icon": icon or "/favicon.svg",
        }
    )

    delivered = 0
    pruned = 0
    failed = 0

    results = await asyncio.gather(
        *(
            _send_one(
                sub_id=r["id"],
                endpoint=r["endpoint"],
                p256dh=r["p256dh"],
                auth=r["auth"],
                payload=payload,
            )
            for r in rows
        ),
        return_exceptions=False,
    )

    for status in results:
        if status == "delivered":
            delivered += 1
        elif status == "pruned":
            pruned += 1
        else:
            failed += 1

    logger.info(
        "[push] broadcast %r: %d/%d delivered · %d pruned · %d failed",
        title, delivered, len(rows), pruned, failed,
    )
    return BroadcastResult(
        attempted=len(rows),
        delivered=delivered,
        pruned=pruned,
        failed=failed,
    )


async def send_to_subscription(
    *,
    subscription_id: int,
    title: str,
    body: str,
    url: str = "/",
    tag: str | None = None,
    icon: str | None = None,
) -> str:
    """Send to one subscription by DB id. Used by /api/push/test."""
    async with async_session() as s:
        r = await s.execute(
            text(
                """
                SELECT id, endpoint, p256dh, auth
                FROM push_subscriptions
                WHERE id = :id AND status = 'active'
                """
            ),
            {"id": subscription_id},
        )
        row = r.first()
    if not row:
        return "no_active_subscription"

    payload = json.dumps(
        {
            "title": title,
            "body": body,
            "url": url,
            "tag": tag or "astra-test",
            "icon": icon or "/favicon.svg",
        }
    )
    return await _send_one(
        sub_id=row[0],
        endpoint=row[1],
        p256dh=row[2],
        auth=row[3],
        payload=payload,
    )


# ──────────────────────────────────────────────────────────────────
# Internals
# ──────────────────────────────────────────────────────────────────


async def _fetch_subscriptions(
    *, include_inactive: bool = False,
) -> list[dict[str, Any]]:
    async with async_session() as s:
        if include_inactive:
            q = text(
                """
                SELECT id, endpoint, p256dh, auth FROM push_subscriptions
                WHERE status IN ('active', 'failed')
                """
            )
        else:
            q = text(
                """
                SELECT id, endpoint, p256dh, auth FROM push_subscriptions
                WHERE status = 'active'
                """
            )
        r = await s.execute(q)
        return [
            {
                "id": row[0],
                "endpoint": row[1],
                "p256dh": row[2],
                "auth": row[3],
            }
            for row in r.all()
        ]


async def _send_one(
    *,
    sub_id: int,
    endpoint: str,
    p256dh: str,
    auth: str,
    payload: str,
) -> str:
    """Attempt one send. Returns 'delivered' | 'pruned' | 'failed'."""
    try:
        status_code, err = await asyncio.to_thread(
            _sync_send, endpoint, p256dh, auth, payload,
        )
    except Exception as e:
        await _mark_failed(sub_id, str(e)[:500])
        return "failed"

    if 200 <= status_code < 300:
        await _mark_success(sub_id)
        return "delivered"
    if status_code in DEAD_STATUS_CODES:
        await _mark_gone(sub_id, f"HTTP {status_code}: {err[:200] if err else ''}")
        return "pruned"
    await _mark_failed(sub_id, f"HTTP {status_code}: {err[:300] if err else ''}")
    return "failed"


_PRIV_KEY_PATH_CACHE: str | None = None


def _vapid_private_key_path() -> str:
    """Resolve a filesystem PATH to the VAPID private-key PEM.

    pywebpush needs a path, not a string. On Railway we only have env
    vars, so if VAPID_PRIVATE_KEY holds the PEM CONTENT, materialize it
    to a temp file once and cache the path (the env→file pattern, same as
    Gmail creds). An explicit VAPID_PRIVATE_KEY_PATH still wins if set.
    Returns "" when no key is configured (push then no-ops, never crashes).
    """
    global _PRIV_KEY_PATH_CACHE
    import os
    import tempfile

    from astra.config import settings

    if settings.vapid_private_key_path:
        return settings.vapid_private_key_path
    if _PRIV_KEY_PATH_CACHE and os.path.exists(_PRIV_KEY_PATH_CACHE):
        return _PRIV_KEY_PATH_CACHE
    pem = os.environ.get("VAPID_PRIVATE_KEY", "").strip()
    if not pem:
        return ""
    # Railway env vars sometimes store newlines as the literal two chars
    # backslash-n; normalize so the PEM parses.
    pem = pem.replace("\\n", "\n")
    if not pem.endswith("\n"):
        pem += "\n"
    fd, path = tempfile.mkstemp(prefix="vapid_", suffix=".pem")
    with os.fdopen(fd, "w") as f:
        f.write(pem)
    _PRIV_KEY_PATH_CACHE = path
    return path


def _sync_send(
    endpoint: str, p256dh: str, auth: str, payload: str,
) -> tuple[int, str | None]:
    """Blocking pywebpush call. Runs in a thread.

    pywebpush accepts a Vapid *instance* or a *path* to a PEM — NOT a
    PEM string. Passing the PEM string fails with ASN.1 parsing errors
    because pywebpush tries to base64-decode it as a raw key.
    """
    from pywebpush import WebPushException, webpush

    from astra.config import settings

    priv_path = _vapid_private_key_path()
    contact = settings.vapid_contact or "mailto:astra@localhost"
    if not priv_path:
        return 0, "VAPID private key not configured"

    subscription_info = {
        "endpoint": endpoint,
        "keys": {"p256dh": p256dh, "auth": auth},
    }

    # Pass the path, not the PEM contents. pywebpush reads the file and
    # hands it to py_vapid.Vapid internally with the right loader.
    try:
        resp = webpush(
            subscription_info=subscription_info,
            data=payload,
            vapid_private_key=priv_path,
            vapid_claims={"sub": contact},
            ttl=3600,
        )
        return getattr(resp, "status_code", 201), None
    except WebPushException as e:
        sc = 0
        body = ""
        if e.response is not None:
            sc = e.response.status_code
            try:
                body = e.response.text
            except Exception:
                body = ""
        return sc, body or str(e)
    except Exception as e:
        return 0, str(e)


async def _mark_success(sub_id: int) -> None:
    async with async_session() as s:
        await s.execute(
            text(
                """
                UPDATE push_subscriptions
                SET status='active',
                    failure_count=0,
                    last_success_at=now(),
                    last_seen_at=now(),
                    last_error=NULL
                WHERE id=:id
                """
            ),
            {"id": sub_id},
        )
        await s.commit()


async def _mark_failed(sub_id: int, err: str) -> None:
    async with async_session() as s:
        await s.execute(
            text(
                """
                UPDATE push_subscriptions
                SET failure_count = failure_count + 1,
                    last_failure_at = now(),
                    last_error = :e,
                    -- After 10 consecutive failures, treat as gone so we
                    -- stop wasting RTTs on it.
                    status = CASE
                      WHEN failure_count + 1 >= 10 THEN 'gone'
                      ELSE status
                    END
                WHERE id=:id
                """
            ),
            {"id": sub_id, "e": err},
        )
        await s.commit()


async def _mark_gone(sub_id: int, err: str) -> None:
    async with async_session() as s:
        await s.execute(
            text(
                """
                UPDATE push_subscriptions
                SET status='gone',
                    last_failure_at=now(),
                    last_error=:e
                WHERE id=:id
                """
            ),
            {"id": sub_id, "e": err},
        )
        await s.commit()
