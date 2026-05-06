"""
Preview store — durable storage for agent-generated content the
user wants to open in a tab.

The agent calls prepare_preview(content, title, content_type, ttl)
→ this module saves to the `previews` table and returns the row's
UUID. The web UI then uses /api/preview/<id> (which proxies to the
stream service's GET /previews/<id>) to render the content with the
right Content-Type and security headers.

Default TTL: 7 days. Long enough that someone can come back to a
preview tomorrow; short enough that we don't accumulate forever.
The GET route returns 410 Gone for expired rows; a background sweep
hard-deletes.

API:
    create_preview(title, body, content_type, ttl_seconds) -> str
    get_preview(preview_id) -> dict | None  (None if missing/expired)
    sweep_expired() -> int
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from astra.db.engine import async_session

logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 7 * 24 * 60 * 60  # 7 days
# The body is text and we don't want to store huge dumps. 10MB
# matches the proxy's effective bound and keeps DB rows reasonable.
MAX_BODY_BYTES = 10 * 1024 * 1024


async def create_preview(
    *,
    title: str,
    body: str,
    content_type: str = "text/html; charset=utf-8",
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> str:
    """Insert a row, return its UUID as a string.

    Body length is enforced (raises ValueError if too large). Title
    is allowed to be empty — the UI falls back to a generic label.
    """
    if not body:
        raise ValueError("preview body cannot be empty")
    if len(body.encode("utf-8")) > MAX_BODY_BYTES:
        raise ValueError(
            f"preview body exceeds {MAX_BODY_BYTES} bytes "
            f"({len(body.encode('utf-8'))} given)"
        )
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
    async with async_session() as s:
        r = await s.execute(
            text(
                """
                INSERT INTO previews (title, content_type, body, expires_at)
                VALUES (:t, :ct, :b, :exp)
                RETURNING id
                """
            ),
            {
                "t": title or "",
                "ct": content_type,
                "b": body,
                "exp": expires_at,
            },
        )
        row = r.first()
        await s.commit()
    if not row:
        raise RuntimeError("preview insert returned no row")
    return str(row.id)


async def get_preview(preview_id: str) -> dict[str, Any] | None:
    """Fetch a preview by id. Returns None if missing or expired.

    The route layer should map None → 404 and the expired case is
    indistinguishable to the user — both mean "not viewable now."
    """
    if not preview_id:
        return None
    async with async_session() as s:
        r = await s.execute(
            text(
                """
                SELECT id, title, content_type, body, created_at, expires_at
                FROM previews
                WHERE id = CAST(:id AS uuid)
                  AND expires_at > now()
                """
            ),
            {"id": preview_id},
        )
        row = r.first()
    if not row:
        return None
    return {
        "id": str(row.id),
        "title": row.title or "",
        "content_type": row.content_type,
        "body": row.body,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
    }


async def sweep_expired() -> int:
    """Hard-delete rows past their expires_at. Returns count deleted.

    Idempotent. Safe to run on a cron. Not strictly necessary for
    correctness (get_preview filters by expires_at) — just keeps the
    table from growing unboundedly.
    """
    async with async_session() as s:
        r = await s.execute(
            text("DELETE FROM previews WHERE expires_at < now()")
        )
        await s.commit()
    return r.rowcount or 0
