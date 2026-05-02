"""
Persistence layer for creator artifacts.

Every deck/doc/one-pager/brand-kit/critique that the creator tools
produce gets a row in `creator_artifacts`. Callers write structured
JSON content; renderers later read that content and produce binaries
(PDF/PPTX) which they store in R2 and update r2_pdf_key /
r2_pptx_key on the row.

Why a single table for all kinds (deck, doc, one_pager, etc.):
the metadata (business, audience, ask, parent) is identical;
the content shape is what differs. JSONB lets us evolve the
content schema per-kind without DB migrations.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import text

from astra.db.engine import async_session


async def create_artifact(
    *,
    business_slug: str,
    kind: str,
    title: str,
    content: dict[str, Any],
    audience_slug: str | None = None,
    ask: str = "",
    parent_id: int | None = None,
) -> dict[str, Any]:
    """Insert a new artifact and return its row as a dict.

    Returns the saved row including id and timestamps so the caller
    can immediately reference the new artifact (e.g. include the id
    in the agent's reply text).
    """
    async with async_session() as s:
        r = await s.execute(
            text(
                """
                INSERT INTO creator_artifacts
                  (business_slug, kind, audience_slug, title, ask,
                   content, parent_id)
                VALUES
                  (:bs, :k, :aud, :t, :ask, CAST(:c AS JSONB), :p)
                RETURNING id, created_at
                """
            ),
            {
                "bs": business_slug[:63],
                "k": kind[:31],
                "aud": (audience_slug or None) and audience_slug[:127],
                "t": title[:511],
                "ask": ask or "",
                "c": json.dumps(content),
                "p": parent_id,
            },
        )
        row = r.one()
        await s.commit()
    return {
        "id": int(row[0]),
        "created_at": row[1].isoformat() if row[1] else None,
        "business_slug": business_slug,
        "kind": kind,
        "audience_slug": audience_slug,
        "title": title,
        "ask": ask,
        "content": content,
        "parent_id": parent_id,
    }


async def get_artifact(artifact_id: int) -> dict[str, Any] | None:
    async with async_session() as s:
        r = await s.execute(
            text(
                """
                SELECT id, business_slug, kind, audience_slug, title,
                       ask, content, parent_id, r2_pdf_key, r2_pptx_key,
                       created_at, updated_at
                FROM creator_artifacts WHERE id = :id
                """
            ),
            {"id": int(artifact_id)},
        )
        row = r.first()
    if not row:
        return None
    return {
        "id": row[0],
        "business_slug": row[1],
        "kind": row[2],
        "audience_slug": row[3],
        "title": row[4],
        "ask": row[5],
        "content": row[6] or {},
        "parent_id": row[7],
        "r2_pdf_key": row[8] or "",
        "r2_pptx_key": row[9] or "",
        "created_at": row[10].isoformat() if row[10] else None,
        "updated_at": row[11].isoformat() if row[11] else None,
    }


async def list_artifacts(
    *,
    business_slug: str | None = None,
    kind: str | None = None,
    limit: int = 25,
) -> list[dict[str, Any]]:
    """Newest-first listing, optionally filtered by business or kind."""
    where: list[str] = []
    params: dict[str, Any] = {"lim": max(1, min(200, limit))}
    if business_slug:
        where.append("business_slug = :bs")
        params["bs"] = business_slug
    if kind:
        where.append("kind = :k")
        params["k"] = kind
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    async with async_session() as s:
        r = await s.execute(
            text(
                f"""
                SELECT id, business_slug, kind, audience_slug, title,
                       ask, r2_pdf_key, r2_pptx_key, created_at
                FROM creator_artifacts
                {clause}
                ORDER BY created_at DESC
                LIMIT :lim
                """
            ),
            params,
        )
        rows = r.all()
    return [
        {
            "id": row[0],
            "business_slug": row[1],
            "kind": row[2],
            "audience_slug": row[3],
            "title": row[4],
            "ask": row[5],
            "r2_pdf_key": row[6] or "",
            "r2_pptx_key": row[7] or "",
            "created_at": row[8].isoformat() if row[8] else None,
        }
        for row in rows
    ]


async def update_artifact_render_key(
    artifact_id: int, *, kind: str, key: str
) -> bool:
    """Set the R2 object key for a rendered binary.

    `kind` is "pdf" or "pptx" — chooses the column.
    """
    col = {"pdf": "r2_pdf_key", "pptx": "r2_pptx_key"}.get(kind)
    if not col:
        raise ValueError(f"unknown render kind: {kind}")
    async with async_session() as s:
        r = await s.execute(
            text(
                f"UPDATE creator_artifacts SET {col} = :k, updated_at = now() "
                f"WHERE id = :id"
            ),
            {"id": int(artifact_id), "k": key or ""},
        )
        await s.commit()
        return (r.rowcount or 0) > 0
