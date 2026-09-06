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
        # Cloud container: Notes lives on the Mac. File a notes.sync
        # intent with the capability broker and wait a bounded time.
        # Previously this path silently returned all-zeros and the
        # agent told Kunal it had "live checked"; then it ran the
        # harvester through the Mac bridge, retired in Phase A6.
        # Never a fake success: every outcome below says what it is.
        return await _sync_via_body_for_chat()
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


# Chat-path wait. The registry timeout for notes_sync is this plus a
# 20 s margin: astra/runtime/sdk_adapter.py SLOW_EXACT["notes_sync"] is
# 110, pinned to this constant by
# tests/test_runtime/test_timeout_hierarchy_broker.py, and both sit
# under the 240 s turn cap. Without that entry the registry's default
# 15 s cancels the tool mid-wait and the model reports a failed sync
# while the executor is still running it. The scheduler job, which runs
# outside any turn, waits 240 s. An auto verb resolves in 6-12 s when
# the body is awake, so the wait only matters when it is not.
_CHAT_WAIT_SEC = 90


async def sync_via_body(*, why: str, wait_sec: int):
    """File a `notes.sync` intent for the Mac body and wait for it.

    The single chokepoint for both the chat tool and the scheduler
    job. `notes.sync` takes no arguments, so no model input reaches
    the body. `actor` is a keyword the model cannot set; it names this
    caller in the audit line. Returns the client's IntentResult
    untouched; callers decide how to report it, and none of them may
    report an unfinished or refused intent as a sync that happened.
    """
    from astra.broker.client import run_intent

    return await run_intent(
        "notes.sync", {}, why=why, actor="notes_sync", wait_sec=wait_sec,
    )


async def _sync_via_body_for_chat() -> dict:
    """The chat tool's cloud path: file, wait a bounded time, and say
    exactly what happened.

    Branches on `IntentResult.refusal_code`, never on the wording of
    `deny_reason`: the first version matched `"not wired" in reason`,
    the EXECUTOR's phrase, while the client's own pre-filing refusal
    said "cannot perform ... yet", so the unwired case fell through to
    the offline branch and told Kunal to open a laptop that was already
    open and polling. Each refusal code gets the remedy that can
    actually work; only a FILED row's deny_reason carries executor text.
    """
    try:
        res = await sync_via_body(
            why="Apple Notes mirror refresh (asked in chat)",
            wait_sec=_CHAT_WAIT_SEC,
        )
    except Exception as e:
        return _text((
            "Could not ask Kunal's Mac to sync Apple Notes: the broker "
            f"client is unavailable ({e}). The mirror stands as of its "
            "last sync; say so."
        ), error=True)

    status = str(getattr(res, "status", "") or "").lower()
    reason = str(getattr(res, "deny_reason", None) or "")
    code = str(getattr(res, "refusal_code", "") or "")
    iid = getattr(res, "intent_id", None)
    last = await _last_synced_at()
    stands = f"The mirror stands as of its last sync ({last})."

    if status == "succeeded":
        raw = getattr(res, "result_bytes", None) or b""
        out = bytes(raw).decode("utf-8", "replace").strip()[:600]
        return _text(
            f"Apple Notes sync ran on Kunal's Mac (intent #{iid}, receipt "
            f"{getattr(res, 'receipt_verdict', 'unknown')}):\n{out}"
        )
    if status in _OPEN_STATES:
        return _text(
            f"The sync is filed (intent #{iid}) but the Mac has not "
            f"finished it yet. {stands} Tell Kunal that, and check the "
            "intent later with poll_status rather than re-filing."
        )
    if status in ("denied", "failed", "expired"):
        # A FILED row: deny_reason is what the broker or executor wrote.
        return _text((
            f"Apple Notes sync did not happen (intent #{iid} {status}: "
            f"{reason or 'no reason given'}). {stands}"
        ), error=True)

    # Refused by the client before anything was filed. The code names
    # the situation; the remedy must be one that can work.
    if code == "unwired":
        return _text((
            "Cannot sync Apple Notes yet: the notes.sync verb is in the "
            "body's catalogue but not wired in the executor (it waits on "
            "the GUI-session helper that drives Notes.app, which does not "
            f"exist yet). {stands} Tell Kunal plainly; opening or waking "
            "the Mac changes nothing, and do not retry. Offer to add_task "
            "it tagged 'body' if it matters."
        ), error=True)
    if code == "busy":
        return _text((
            f"Cannot sync Apple Notes right now: {reason} {stands} The Mac "
            "is up; the broker serves one intent at a time and is inside "
            "another one, so nothing would claim this yet. Tell Kunal it "
            "is busy with that intent, not asleep, and offer to try again "
            "once it resolves or to add_task it tagged 'body'."
        ), error=True)
    if code == "offline":
        return _text((
            f"Cannot sync Apple Notes right now: {reason} {stands} The "
            "sync runs on Kunal's Mac; a Mac that is not polling is a "
            "closed laptop, which is normal. Tell Kunal that at this "
            "point of need only, and offer to retry when the Mac is "
            "open or to file a task tagged 'body'."
        ), error=True)
    if code in ("no_body", "ambiguous_body"):
        return _text((
            f"Cannot sync Apple Notes: {reason} {stands} This is an "
            "enrolment problem on the broker, not the laptop being "
            "closed; do not tell Kunal to open it."
        ), error=True)
    if code == "queue_full":
        return _text((
            f"Cannot sync Apple Notes right now: {reason} {stands} Do not "
            "add to the queue; tell Kunal it is backed up."
        ), error=True)
    # 'args', 'signed_outside_turn', 'unknown_verb' cannot happen for
    # notes.sync (no args, auto, catalogued); anything else is a
    # client bug, reported as such rather than dressed as the laptop.
    return _text((
        "Cannot sync Apple Notes: the broker client refused before "
        f"filing ({code or 'no code'}: {reason or status or 'no reason'}). "
        f"{stands} This is not a closed laptop; report it as a client "
        "refusal."
    ), error=True)


_OPEN_STATES = frozenset({"pending", "claimed", "awaiting_human", "running"})


def _text(msg: str, *, error: bool = False) -> dict:
    out = {"content": [{"type": "text", "text": msg}]}
    if error:
        out["is_error"] = True
    return out


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
