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
    lines = [
        f"{stats['total_notes']} notes total across {len(stats['by_folder'])} folders.",
        f"Folders: {stats['by_folder']}",
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
    force = bool(args.get("force", False))
    report = await sync_all(force=force)
    lines = [
        f"Apple Notes sync · {report.elapsed_ms}ms",
        f"  seen: {report.total_notes_seen}",
        f"  new: {report.new_notes}",
        f"  updated: {report.updated_notes}",
        f"  unchanged: {report.unchanged_notes}",
        f"  failed: {report.failed_notes}",
    ]
    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


def create_notes_mcp_server():
    return create_sdk_mcp_server(
        name="astra-notes",
        version="0.1.0",
        tools=[notes_search_tool, notes_list_tool, notes_get_tool, notes_sync_tool],
    )
