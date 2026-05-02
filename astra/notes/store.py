"""
Persistence helpers for Apple Notes. The harvester calls `upsert_note`
once per synced note; MCP tools and the web API call the read helpers.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from astra.notes.models import AppleNote


async def get_note_by_apple_id(
    session: AsyncSession, apple_id: str
) -> AppleNote | None:
    stmt = select(AppleNote).where(AppleNote.apple_id == apple_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def upsert_note(
    session: AsyncSession,
    *,
    apple_id: str,
    title: str,
    folder: str,
    body_html: str,
    body_text: str,
    content_hash: str,
    created_at_native: datetime | None,
    modified_at_native: datetime | None,
    char_count: int,
) -> AppleNote:
    existing = await get_note_by_apple_id(session, apple_id)
    if existing is None:
        note = AppleNote(
            apple_id=apple_id,
            title=title,
            folder=folder or "",
            body_html=body_html,
            body_text=body_text,
            content_hash=content_hash,
            created_at_native=created_at_native,
            modified_at_native=modified_at_native,
            char_count=char_count,
        )
        session.add(note)
        return note

    existing.title = title
    existing.folder = folder or ""
    existing.body_html = body_html
    existing.body_text = body_text
    existing.content_hash = content_hash
    if created_at_native:
        existing.created_at_native = created_at_native
    if modified_at_native:
        existing.modified_at_native = modified_at_native
    existing.char_count = char_count
    return existing


async def list_notes(
    *,
    folder: str | None = None,
    title_contains: str | None = None,
    limit: int = 50,
    min_chars: int = 0,
) -> list[dict[str, Any]]:
    """List notes ordered by modification (newest first)."""
    from astra.db.engine import async_session

    async with async_session() as session:
        stmt = select(AppleNote).order_by(AppleNote.modified_at_native.desc().nullslast())
        if folder:
            stmt = stmt.where(AppleNote.folder == folder)
        if title_contains:
            stmt = stmt.where(AppleNote.title.ilike(f"%{title_contains}%"))
        if min_chars:
            stmt = stmt.where(AppleNote.char_count >= min_chars)
        stmt = stmt.limit(limit)
        result = await session.execute(stmt)
        return [_to_dict(n) for n in result.scalars()]


async def get_note(note_id: int) -> dict[str, Any] | None:
    from astra.db.engine import async_session

    async with async_session() as session:
        note = await session.get(AppleNote, note_id)
        return _to_dict(note) if note else None


async def search_notes(
    query: str, *, limit: int = 10
) -> list[dict[str, Any]]:
    """Plaintext substring search across title + body. Cheap and fast.

    For semantic search we'd need embeddings (possible next step,
    same pattern as `memories`). For now keyword is enough for the
    "find missed sessions" use case.
    """
    from astra.db.engine import async_session

    async with async_session() as session:
        needle = f"%{query}%"
        stmt = (
            select(AppleNote)
            .where(
                or_(
                    AppleNote.title.ilike(needle),
                    AppleNote.body_text.ilike(needle),
                )
            )
            .order_by(AppleNote.modified_at_native.desc().nullslast())
            .limit(limit)
        )
        result = await session.execute(stmt)
        return [_to_dict(n, preview=True) for n in result.scalars()]


async def note_stats() -> dict[str, Any]:
    """Quick roll-up for the /api/notes index and briefing signals."""
    from astra.db.engine import async_session

    async with async_session() as session:
        total = (await session.execute(select(func.count()).select_from(AppleNote))).scalar_one()
        total_chars = (
            await session.execute(select(func.coalesce(func.sum(AppleNote.char_count), 0)))
        ).scalar_one()
        most_recent = await session.execute(
            select(AppleNote)
            .order_by(AppleNote.modified_at_native.desc().nullslast())
            .limit(1)
        )
        mr = most_recent.scalar_one_or_none()
        by_folder_rows = await session.execute(
            select(AppleNote.folder, func.count()).group_by(AppleNote.folder)
        )
        by_folder = {row[0]: int(row[1]) for row in by_folder_rows.all()}

    return {
        "total_notes": int(total),
        "total_chars": int(total_chars),
        "by_folder": by_folder,
        "most_recent": _to_dict(mr, preview=True) if mr else None,
    }


def _to_dict(n: AppleNote | None, *, preview: bool = False) -> dict[str, Any]:
    if n is None:
        return {}
    body = n.body_text if not preview else (n.body_text[:300] + ("…" if len(n.body_text) > 300 else ""))
    return {
        "id": n.id,
        "apple_id": n.apple_id,
        "title": n.title,
        "folder": n.folder,
        "char_count": n.char_count,
        "tags": n.tags,
        "body_text": body,
        "created_at_native": n.created_at_native.isoformat() if n.created_at_native else None,
        "modified_at_native": n.modified_at_native.isoformat() if n.modified_at_native else None,
        "last_synced_at": n.last_synced_at.isoformat() if n.last_synced_at else None,
    }
