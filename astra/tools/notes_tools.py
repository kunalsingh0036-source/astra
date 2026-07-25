"""
MCP tools for Apple Notes — give Astra read access to Kunal's
personal notes (training tracking, missed sessions, context).

Four tools:
  - notes_search(query)   → substring search across title + body
  - notes_list(folder?)   → recent notes, optionally filtered
  - notes_get(title|id)   → full body of a specific note
  - notes_sync()          → force a re-sync from Notes.app

Sync normally runs in the scheduler every 30 min. The tool is for
"refresh now" moments mid-conversation.
"""

from astra.runtime.sdk_compat import tool, create_sdk_mcp_server

from astra.notes.harvester import sync_all
from astra.notes.store import (
    get_note,
    list_notes,
    note_stats,
    search_notes,
)


@tool(
    "notes_search",
    "Search Kunal's Apple Notes (personal tracking, training log, "
    "missed sessions, context) by substring. Returns matching notes "
    "with a 300-char preview. Use when answering questions about "
    "Kunal's training, missed sessions, preferences, or anything "
    "that's in his notes.",
    {"query": str, "limit": int},
)
async def notes_search_tool(args: dict) -> dict:
    q = (args.get("query") or "").strip()
    if not q:
        return {"content": [{"type": "text", "text": "notes_search: query required"}]}
    limit = max(1, min(20, int(args.get("limit") or 10)))
    rows = await search_notes(q, limit=limit)
    if not rows:
        return {"content": [{"type": "text", "text": f"No notes match: {q}"}]}

    lines = [f"{len(rows)} notes matching {q!r}:"]
    for n in rows:
        mod = (n.get("modified_at_native") or "")[:10]
        lines.append(
            f"\n#{n['id']} · {n['title']} ({n['char_count']} chars · {mod} · {n['folder']})"
        )
        body = n.get("body_text", "").strip()
        if body:
            lines.append(body)
    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


@tool(
    "notes_list",
    "List Kunal's Apple Notes, most recent first. Optionally filter "
    "by folder. Returns title + folder + modification date + char "
    "count for each. Use to discover what notes exist before reading.",
    {"folder": str, "limit": int, "min_chars": int},
)
async def notes_list_tool(args: dict) -> dict:
    folder = args.get("folder") or None
    limit = max(1, min(50, int(args.get("limit") or 20)))
    min_chars = max(0, int(args.get("min_chars") or 0))
    rows = await list_notes(folder=folder, limit=limit, min_chars=min_chars)
    stats = await note_stats()
    last_sync = await _last_synced_at()
    lines = [
        f"{stats['total_notes']} notes in the MIRROR across {len(stats['by_folder'])} folders "
        f"(as of last Mac sync: {last_sync}).",
        f"Folders: {stats['by_folder']}",
        "CAVEAT you must pass on when Kunal asks about counts: this is the "
        "Mac's Notes mirror. Notes created on his iPhone can lag until the "
        "Mac's iCloud catches up. If his count differs, his device is the "
        "truth — offer a fresh sync (notes_sync), never re-assert this number.",
        "",
        f"Showing {len(rows)} (folder={folder or 'any'}, min_chars={min_chars}):",
    ]
    for n in rows:
        mod = (n.get("modified_at_native") or "")[:10]
        lines.append(
            f"  #{n['id']:<4} {n['char_count']:>5} chars · {mod} · [{n['folder']}] {n['title']}"
        )
    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


@tool(
    "notes_get",
    "Read the full body of a specific Apple Note by its DB id. Use "
    "after notes_list or notes_search identifies the right note.",
    {"id": int},
)
async def notes_get_tool(args: dict) -> dict:
    note_id = int(args.get("id") or 0)
    if not note_id:
        return {"content": [{"type": "text", "text": "notes_get: id required"}]}
    n = await get_note(note_id)
    if not n:
        return {"content": [{"type": "text", "text": f"Note #{note_id} not found"}]}
    body = n.get("body_text", "")
    header = (
        f"#{n['id']} · {n['title']}\n"
        f"folder: {n['folder']} · {n['char_count']} chars · "
        f"modified: {(n.get('modified_at_native') or '')[:19]}\n"
        f"---"
    )
    return {"content": [{"type": "text", "text": f"{header}\n{body}"}]}


@tool(
    "notes_sync",
    "Force a fresh sync from Apple Notes. Normally runs on schedule "
    "every 30 minutes — use this when Kunal says he just wrote or "
    "edited something and wants Astra to see it immediately.",
    {"force": bool},
)
async def notes_sync_tool(args: dict) -> dict:
    import shutil

    force = bool(args.get("force", False))
    if shutil.which("osascript") is None:
        # Cloud container: Notes lives on the Mac. Route the sync
        # through the bridge (runs the harvester ON the Mac against the
        # cloud DB). Previously this path silently returned all-zeros
        # and the agent told Kunal it had "live checked" — never again.
        return await _bridge_sync(force=force)
    report = await sync_all(force=force)
    lines = [
        f"Apple Notes sync (ran on Mac) · {report.elapsed_ms}ms",
        f"  seen: {report.total_notes_seen}",
        f"  new: {report.new_notes}",
        f"  updated: {report.updated_notes}",
        f"  unchanged: {report.unchanged_notes}",
        f"  failed: {report.failed_notes}",
    ]
    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


async def _bridge_sync(*, force: bool) -> dict:
    """Run the harvester on the Mac via the bridge bash channel."""
    from astra.runtime.tools.local import local_bash_impl

    force_arg = "True" if force else "False"
    cmd = (
        'cd "/Users/kunalsingh/Claude Code/astra" && '
        "DATABASE_URL=$(railway variables --service Postgres --json 2>/dev/null "
        "| python3 -c 'import sys,json;print(json.load(sys.stdin)"
        '["DATABASE_PUBLIC_URL"].replace("postgresql://","postgresql+asyncpg://")'
        ".replace(\"postgres://\",\"postgresql+asyncpg://\"))') "
        '.venv/bin/python3 -c "import asyncio; '
        "from astra.notes.harvester import sync_all; "
        f"r = asyncio.run(sync_all(force={force_arg})); "
        "print(f'seen={r.total_notes_seen} new={r.new_notes} "
        "updated={r.updated_notes} unchanged={r.unchanged_notes} "
        "failed={r.failed_notes}')\""
    )
    res = await local_bash_impl({"command": cmd})
    text_parts = [c.get("text", "") for c in (res.get("content") or [])
                  if isinstance(c, dict)]
    out = "\n".join(text_parts).strip()
    if "BRIDGE_OFFLINE" in out:
        return {"content": [{"type": "text", "text": (
            "Cannot sync Apple Notes right now: the sync runs on Kunal's "
            "Mac and the Mac is offline (normal when the laptop is closed). "
            "The mirror count stands as-of its last sync — tell Kunal that, "
            "and offer to queue the sync for when the Mac is back."
        )}]}
    return {"content": [{"type": "text", "text": f"Apple Notes sync (via Mac bridge):\n{out}"}]}


async def _last_synced_at() -> str:
    from sqlalchemy import text as _sql

    from astra.db.engine import async_session

    try:
        async with async_session() as s:
            v = (await s.execute(
                _sql("SELECT MAX(last_synced_at) FROM apple_notes")
            )).scalar()
        return v.strftime("%Y-%m-%d %H:%M UTC") if v else "never"
    except Exception:
        return "unknown"


def create_notes_mcp_server():
    return create_sdk_mcp_server(
        name="astra-notes",
        version="0.1.0",
        tools=[notes_search_tool, notes_list_tool, notes_get_tool, notes_sync_tool],
    )
