"""
MCP tools that expose Kunal's iOS-shared payloads to the agent.

The shares table is a continuous stream of signal Kunal pushes from
his phone — quotations he wants Astra to see, articles he's read,
PDFs from Naval Forces purchase orders, voice notes after meetings.
Without these tools the agent can't answer "what did I share yesterday?"
or "find the PDF Chinmay sent last week" — the data is in Postgres but
invisible to the conversational surface.

Two tools:

  * `list_recent_shares` — last N shares, most recent first. Includes
    the LLM-written summary, the action taken (memory / task / note),
    and a short head of the actual content. Default window 24h.
  * `search_shares` — substring search across title / text / note /
    extracted_text / source_url. Use when the user asks about a
    specific topic ("the squash article I sent") rather than a window.

Both return compact text — full payloads stay in the DB and surface
through semantic memory search if the agent needs them in depth.
"""

from __future__ import annotations

from astra.runtime.sdk_compat import tool, create_sdk_mcp_server

from astra.shares import get_share, recent_shares_for_briefing, search_shares


def _format_share_row(s: dict) -> str:
    when = (s.get("created_at") or "")[:16].replace("T", " ")
    src = s.get("source_app") or s.get("kind") or "share"
    state_glyph = {
        "filed": "✓",
        "received": "…",
        "processing": "…",
        "error": "✗",
    }.get(s.get("state", ""), "·")
    action = s.get("action_taken") or ""
    action_tag = f" [{action}]" if action else ""
    title = s.get("title") or ""
    summary = s.get("summary") or ""
    head = (s.get("head") or "").strip()

    line = f"{state_glyph} #{s['id']}  {when}  via {src}{action_tag}"
    body_bits: list[str] = []
    if title:
        body_bits.append(title)
    if summary:
        body_bits.append(summary)
    elif head:
        # No LLM summary yet (still processing or classify failed) —
        # quote a snippet so the agent can still reason about it.
        body_bits.append(head[:200] + ("…" if len(head) > 200 else ""))
    if s.get("source_url"):
        body_bits.append(s["source_url"])

    if body_bits:
        line += "\n    " + "\n    ".join(b for b in body_bits if b)
    return line


@tool(
    "list_recent_shares",
    "List the most recent items Kunal pushed into Astra from his iPhone "
    "Share Sheet (PDFs, URLs, notes, quotations, voice memos). "
    "Use when he asks 'what did I share', 'show me what I sent today', "
    "or when summarizing his day. Default window: last 24 hours.",
    {"hours": int, "limit": int},
)
async def list_recent_shares_tool(args: dict) -> dict:
    hours = max(1, min(24 * 14, int(args.get("hours") or 24)))
    limit = max(1, min(50, int(args.get("limit") or 25)))
    rows = await recent_shares_for_briefing(hours=hours, limit=limit)
    if not rows:
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"No shares in the last {hours}h.",
                }
            ]
        }
    head = (
        f"{len(rows)} share{'s' if len(rows) != 1 else ''} in the "
        f"last {hours}h, newest first:"
    )
    body = "\n\n".join(_format_share_row(s) for s in rows)
    return {"content": [{"type": "text", "text": f"{head}\n\n{body}"}]}


@tool(
    "search_shares",
    "Search Kunal's shared items by substring across title, body, "
    "extracted PDF text, fetched URL content, and source URL. "
    "Use when he refers to a specific shared thing ('the article about "
    "Mistral', 'Chinmay's PDF', 'that LinkedIn post I sent you') and "
    "you need to find it. Default lookback: 60 days.",
    {"query": str, "days": int, "limit": int},
)
async def search_shares_tool(args: dict) -> dict:
    query = (args.get("query") or "").strip()
    if not query:
        return {
            "content": [
                {"type": "text", "text": "search_shares: query required"}
            ]
        }
    days = max(1, min(365, int(args.get("days") or 60)))
    limit = max(1, min(50, int(args.get("limit") or 25)))
    rows = await search_shares(query, days=days, limit=limit)
    if not rows:
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"No shares match '{query}' in the last {days}d.",
                }
            ]
        }
    head = (
        f"{len(rows)} share{'s' if len(rows) != 1 else ''} matching "
        f"'{query}' in the last {days}d, newest first:"
    )
    body = "\n\n".join(_format_share_row(s) for s in rows)
    return {"content": [{"type": "text", "text": f"{head}\n\n{body}"}]}


@tool(
    "get_share",
    "Fetch the FULL content of a single shared item by id, including "
    "the complete extracted PDF text or fetched URL body. Use this "
    "when Kunal asks for the content of a specific share ('show me the "
    "BAY deck', 'read me the article I shared', 'what does the PDF say "
    "about pricing'). list_recent_shares and search_shares return only "
    "a 600-char head; this is the escape hatch when the user needs the "
    "actual material to reason over. The id is what those tools return.",
    {"id": int},
)
async def get_share_tool(args: dict) -> dict:
    sid = int(args.get("id") or 0)
    if not sid:
        return {
            "content": [
                {"type": "text", "text": "get_share: id required"}
            ]
        }
    row = await get_share(sid)
    if row is None:
        return {
            "content": [
                {"type": "text", "text": f"share #{sid} not found"}
            ]
        }
    extracted = row.get("extracted_text") or ""
    head_lines: list[str] = [
        f"Share #{row['id']}",
        f"  kind:        {row['kind']}",
        f"  source_app:  {row['source_app']}",
        f"  source_url:  {row['source_url'] or '(none)'}",
        f"  title:       {row['title'] or '(none)'}",
        f"  state:       {row['state']}",
        f"  action:      {row['action_taken'] or '(none)'}",
        f"  summary:     {row['summary'] or '(none)'}",
        f"  created_at:  {row['created_at']}",
    ]
    if row.get("file_path"):
        head_lines.append(f"  file_path:   {row['file_path']}")
    if row.get("mime_type"):
        head_lines.append(f"  mime:        {row['mime_type']}")
    if extracted:
        head_lines.append(f"  extracted:   {len(extracted)} chars")
    head = "\n".join(head_lines)

    body_parts: list[str] = []
    if row.get("text"):
        body_parts.append(f"=== text (as posted) ===\n{row['text']}")
    if row.get("note"):
        body_parts.append(f"=== note ===\n{row['note']}")
    if extracted:
        body_parts.append(f"=== extracted content ===\n{extracted}")

    full = head
    if body_parts:
        full += "\n\n" + "\n\n".join(body_parts)
    return {"content": [{"type": "text", "text": full}]}


def create_shares_mcp_server():
    return create_sdk_mcp_server(
        name="astra-shares",
        version="0.1.0",
        tools=[
            list_recent_shares_tool,
            search_shares_tool,
            get_share_tool,
        ],
    )
