"""
Thin HTTP client over email-agent.

email-agent owns Gmail OAuth, Pub/Sub push, message storage, contact
records, drafts, and templates. We just read — and sometimes ask it
to send on our behalf via /messages/send, which already exists.

All calls are async httpx, 5s default timeout. Every function returns
an empty list / dict on error so callers don't have to guard against
a single down service.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


BASE_URL = "http://localhost:8005"
DEFAULT_TIMEOUT = 5.0


async def _get(path: str, params: dict[str, Any] | None = None) -> Any:
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as c:
            r = await c.get(f"{BASE_URL}{path}", params=params or {})
            if r.status_code != 200:
                logger.warning("[email] GET %s → %s", path, r.status_code)
                return None
            return r.json()
    except Exception as e:
        logger.warning("[email] GET %s error: %s", path, e)
        return None


async def get_summary() -> dict[str, Any]:
    """Inbox totals — total / unread / action_needed / by_category."""
    data = await _get("/api/v1/messages/summary")
    return data or {}


async def list_messages(
    *,
    direction: str | None = None,
    unread_only: bool = False,
    action_needed_only: bool = False,
    limit: int = 25,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """List MessageOut rows. direction is 'inbound' or 'outbound'."""
    params: dict[str, Any] = {
        "limit": min(max(1, limit), 200),
        "offset": max(0, offset),
    }
    if direction in ("inbound", "outbound"):
        params["direction"] = direction
    if unread_only:
        params["unread_only"] = "true"
    if action_needed_only:
        params["action_needed_only"] = "true"
    data = await _get("/api/v1/messages/", params=params)
    return data if isinstance(data, list) else []


async def get_message(message_id: str) -> dict[str, Any] | None:
    return await _get(f"/api/v1/messages/{message_id}")


async def search_messages(
    query: str,
    *,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Client-side substring search over the last 200 messages.

    email-agent doesn't expose full-text search, so we fetch a window
    and filter locally. Enough for person + subject lookups; we'll
    swap to a real index if usage demands it.
    """
    q = query.strip().lower()
    if not q:
        return []
    # Grab a wide window; we don't know direction upfront.
    msgs = await list_messages(direction=None, limit=200)
    out: list[dict[str, Any]] = []
    for m in msgs:
        haystack = " ".join([
            m.get("subject", "") or "",
            m.get("from_address", "") or "",
            " ".join(m.get("to_addresses", []) or []),
            m.get("snippet", "") or "",
            m.get("body_text", "") or "",
        ]).lower()
        if q in haystack:
            out.append(m)
            if len(out) >= limit:
                break
    return out


async def contacts_list(limit: int = 100) -> list[dict[str, Any]]:
    data = await _get("/api/v1/contacts/", params={"limit": limit})
    return data if isinstance(data, list) else []
