"""
DB layer for share_tokens + shares.

Tokens are 32-byte secrets base64url-encoded (43 chars). Stored in
plaintext because they live behind auth to create, and behind the
share endpoint to use — neither path is shared with untrusted readers.
If we ever expose a public registration flow we'll hash them.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text

from astra.db.engine import async_session

logger = logging.getLogger(__name__)


SHARES_DIR = Path(os.path.expanduser("~/Astra/shares"))
SHARES_DIR.mkdir(parents=True, exist_ok=True)


def _gen_token() -> str:
    return secrets.token_urlsafe(32)


# ──────────────────────────────────────────────────────────────────
# Tokens
# ──────────────────────────────────────────────────────────────────


async def create_token(device_label: str = "iPhone") -> dict[str, Any]:
    tok = _gen_token()
    async with async_session() as s:
        row = await s.execute(
            text(
                """
                INSERT INTO share_tokens (token, device_label, status)
                VALUES (:t, :d, 'active')
                RETURNING id, created_at
                """
            ),
            {"t": tok, "d": device_label[:255]},
        )
        r = row.one()
        await s.commit()
    return {
        "id": int(r[0]),
        "token": tok,
        "device_label": device_label,
        "created_at": r[1].isoformat() if r[1] else None,
    }


async def revoke_token(token_id: int) -> bool:
    async with async_session() as s:
        r = await s.execute(
            text(
                """
                UPDATE share_tokens
                SET status = 'revoked', revoked_at = now()
                WHERE id = :id AND status = 'active'
                """
            ),
            {"id": token_id},
        )
        await s.commit()
        return (r.rowcount or 0) > 0


async def validate_token(token: str) -> int | None:
    """Return the active token's id, or None if unknown/revoked."""
    async with async_session() as s:
        r = await s.execute(
            text(
                """
                SELECT id FROM share_tokens
                WHERE token = :t AND status = 'active'
                """
            ),
            {"t": token},
        )
        row = r.first()
        if not row:
            return None
        # Touch last_used_at so revoked-but-still-in-hand tokens are
        # obvious in the UI.
        await s.execute(
            text(
                "UPDATE share_tokens SET last_used_at = now() WHERE id = :id"
            ),
            {"id": row[0]},
        )
        await s.commit()
        return int(row[0])


async def list_tokens() -> list[dict[str, Any]]:
    async with async_session() as s:
        r = await s.execute(
            text(
                """
                SELECT id, device_label, status, created_at, last_used_at, revoked_at
                FROM share_tokens ORDER BY created_at DESC
                """
            )
        )
        return [
            {
                "id": row[0],
                "device_label": row[1],
                "status": row[2],
                "created_at": row[3].isoformat() if row[3] else None,
                "last_used_at": row[4].isoformat() if row[4] else None,
                "revoked_at": row[5].isoformat() if row[5] else None,
            }
            for row in r.all()
        ]


# ──────────────────────────────────────────────────────────────────
# Share payloads
# ──────────────────────────────────────────────────────────────────


async def file_share_payload(
    *,
    token_id: int | None,
    kind: str,
    source_app: str = "",
    source_url: str = "",
    title: str = "",
    text: str = "",
    note: str = "",
    file_bytes: bytes | None = None,
    file_ext: str = "",
    mime_type: str = "",
    client_ts: datetime | None = None,
) -> dict[str, Any]:
    """Insert a share row; if file_bytes is set, persist it under
    ~/Astra/shares/ and record the path on the row.

    `client_ts` is the moment the iOS extension stamped the share at
    capture time. Pass it through whenever the device knows it — that
    way an outbox-retried share lands with its original moment, not
    whenever the network finally cooperated.
    """
    from sqlalchemy import text as _text

    file_path = ""
    if file_bytes:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        safe_ext = (file_ext or "bin").lstrip(".")[:8]
        fname = f"{ts}_{secrets.token_hex(6)}.{safe_ext}"
        out = SHARES_DIR / fname
        out.write_bytes(file_bytes)
        file_path = str(out)

    async with async_session() as s:
        r = await s.execute(
            _text(
                """
                INSERT INTO shares
                  (token_id, kind, source_app, source_url, title,
                   text, note, file_path, mime_type, state, client_ts)
                VALUES
                  (:tok, :k, :sa, :su, :ti,
                   :tx, :n, :fp, :mt, 'received', :cts)
                RETURNING id, created_at
                """
            ),
            {
                "tok": token_id,
                "k": (kind or "text")[:15],
                "sa": source_app[:127],
                "su": source_url,
                "ti": title[:511],
                "tx": text or "",
                "n": note or "",
                "fp": file_path,
                "mt": mime_type[:127],
                "cts": client_ts,
            },
        )
        row = r.one()
        await s.commit()
        return {
            "id": int(row[0]),
            "created_at": row[1].isoformat() if row[1] else None,
            "file_path": file_path,
        }


async def list_shares(limit: int = 50) -> list[dict[str, Any]]:
    async with async_session() as s:
        r = await s.execute(
            text(
                """
                SELECT id, kind, source_app, source_url, title, text,
                       note, file_path, mime_type, state, summary,
                       action_taken, memory_id, task_ids, error,
                       created_at, processed_at, extracted_text,
                       retry_count, client_ts
                FROM shares ORDER BY created_at DESC LIMIT :lim
                """
            ),
            {"lim": max(1, min(200, limit))},
        )
        rows = []
        for row in r.all():
            rows.append({
                "id": row[0],
                "kind": row[1],
                "source_app": row[2],
                "source_url": row[3],
                "title": row[4],
                "text": row[5],
                "note": row[6],
                "file_path": row[7],
                "mime_type": row[8],
                "state": row[9],
                "summary": row[10],
                "action_taken": row[11],
                "memory_id": row[12],
                "task_ids": row[13] or [],
                "error": row[14],
                "created_at": row[15].isoformat() if row[15] else None,
                "processed_at": row[16].isoformat() if row[16] else None,
                "extracted_text": row[17] or "",
                "retry_count": int(row[18] or 0),
                "client_ts": row[19].isoformat() if row[19] else None,
            })
        return rows


# ──────────────────────────────────────────────────────────────────
# Queries used by the agent + research briefing
# ──────────────────────────────────────────────────────────────────


async def recent_shares_for_briefing(
    *,
    hours: int = 24,
    limit: int = 25,
) -> list[dict[str, Any]]:
    """Compact view for the research/briefing prompt.

    Returns just the fields the briefing needs: when, what kind, where
    from, the LLM-written summary, the action taken, and a short head
    of the extracted/text content. The full payload stays out of the
    briefing budget — semantic search picks it up when needed."""
    async with async_session() as s:
        r = await s.execute(
            text(
                """
                SELECT id, kind, source_app, source_url, title,
                       state, summary, action_taken, created_at,
                       LEFT(COALESCE(NULLIF(extracted_text, ''), text), 600)
                FROM shares
                WHERE created_at >= now() - (:hrs || ' hours')::interval
                ORDER BY created_at DESC
                LIMIT :lim
                """
            ),
            {"hrs": str(hours), "lim": max(1, min(200, limit))},
        )
        out: list[dict[str, Any]] = []
        for row in r.all():
            out.append({
                "id": row[0],
                "kind": row[1],
                "source_app": row[2] or "",
                "source_url": row[3] or "",
                "title": row[4] or "",
                "state": row[5],
                "summary": row[6] or "",
                "action_taken": row[7] or "",
                "created_at": row[8].isoformat() if row[8] else None,
                "head": row[9] or "",
            })
        return out


async def get_share(share_id: int) -> dict[str, Any] | None:
    """Return the full row for a single share, including the entire
    extracted_text. The list/search views truncate to 600 chars for
    prompt economy; this is the escape hatch for when the agent or UI
    needs the actual content. None if the id doesn't exist."""
    async with async_session() as s:
        r = await s.execute(
            text(
                """
                SELECT id, kind, source_app, source_url, title, text,
                       note, file_path, mime_type, state, summary,
                       action_taken, memory_id, task_ids, error,
                       created_at, processed_at, extracted_text,
                       retry_count, client_ts
                FROM shares WHERE id = :id
                """
            ),
            {"id": int(share_id)},
        )
        row = r.first()
        if not row:
            return None
        return {
            "id": row[0],
            "kind": row[1],
            "source_app": row[2],
            "source_url": row[3],
            "title": row[4],
            "text": row[5],
            "note": row[6],
            "file_path": row[7],
            "mime_type": row[8],
            "state": row[9],
            "summary": row[10],
            "action_taken": row[11],
            "memory_id": row[12],
            "task_ids": row[13] or [],
            "error": row[14],
            "created_at": row[15].isoformat() if row[15] else None,
            "processed_at": row[16].isoformat() if row[16] else None,
            "extracted_text": row[17] or "",
            "retry_count": int(row[18] or 0),
            "client_ts": row[19].isoformat() if row[19] else None,
        }


async def search_shares(
    query: str,
    *,
    days: int = 60,
    limit: int = 25,
) -> list[dict[str, Any]]:
    """Substring search across title, text, note, extracted_text, and
    source_url. Case-insensitive. The agent uses this when Kunal asks
    things like 'what did Chinmay send me last week?' — the answer
    comes from shares, not just memory."""
    if not query.strip():
        return []
    async with async_session() as s:
        r = await s.execute(
            text(
                """
                SELECT id, kind, source_app, source_url, title,
                       state, summary, action_taken, created_at,
                       LEFT(COALESCE(NULLIF(extracted_text, ''), text), 600)
                FROM shares
                WHERE created_at >= now() - (:days || ' days')::interval
                  AND (
                    title ILIKE :q OR
                    text ILIKE :q OR
                    note ILIKE :q OR
                    extracted_text ILIKE :q OR
                    source_url ILIKE :q OR
                    summary ILIKE :q
                  )
                ORDER BY created_at DESC
                LIMIT :lim
                """
            ),
            {
                "q": f"%{query.strip()}%",
                "days": str(days),
                "lim": max(1, min(100, limit)),
            },
        )
        out: list[dict[str, Any]] = []
        for row in r.all():
            out.append({
                "id": row[0],
                "kind": row[1],
                "source_app": row[2] or "",
                "source_url": row[3] or "",
                "title": row[4] or "",
                "state": row[5],
                "summary": row[6] or "",
                "action_taken": row[7] or "",
                "created_at": row[8].isoformat() if row[8] else None,
                "head": row[9] or "",
            })
        return out
