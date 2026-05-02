"""
MCP tools so Astra Core can read the inbox intelligently.

Four tools, read-only. Sending still goes through email-agent's
approval-gated path; nothing here writes.
"""

from __future__ import annotations

from claude_agent_sdk import create_sdk_mcp_server, tool

from astra.email.client import list_messages, search_messages
from astra.email.signals import (
    daily_digest,
    top_senders_window,
    unanswered_incoming,
)


@tool(
    "email_digest",
    "Get a digest of inbound email over a window. Filters out noise "
    "(noreply/newsletters/bank alerts). Returns totals + notable "
    "messages with sender + subject + snippet. Use for briefings or "
    "when Kunal asks 'anything new in my inbox?'",
    {"window_hours": int},
)
async def email_digest_tool(args: dict) -> dict:
    hours = max(1, min(168, int(args.get("window_hours") or 24)))
    d = await daily_digest(window_hours=hours)
    lines = [
        f"inbox · last {hours}h",
        f"  {d['real_inbound']} real inbound ({d['noise_count']} noise filtered)",
        f"  {d['unread']} unread · {d['action_needed']} marked action_needed",
        f"  categories: {d['by_category']}",
        "",
        "notable:",
    ]
    if not d["notable"]:
        lines.append("  (nothing stood out)")
    for m in d["notable"]:
        flag = "!" if m["action_needed"] else ("•" if not m["is_read"] else " ")
        lines.append(
            f"  {flag} {(m['sent_at'] or '')[:16]}  {m['from'][:42]:42s}  {m['subject'][:60]}"
        )
    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


@tool(
    "email_unanswered",
    "List inbound messages that haven't been replied to. Sorted by "
    "action_needed, unread, age. Use when Kunal asks 'what's owed "
    "to whom' or when composing the evening briefing's follow-up "
    "list. Excludes noreply senders.",
    {"days": int},
)
async def email_unanswered_tool(args: dict) -> dict:
    days = max(1, min(60, int(args.get("days") or 14)))
    rows = await unanswered_incoming(days=days)
    if not rows:
        return {"content": [{"type": "text",
                              "text": f"inbox clean — no unanswered mail in last {days}d"}]}
    lines = [f"unanswered · last {days}d · {len(rows)} items"]
    for m in rows[:20]:
        flag = "!" if m["action_needed"] else ("•" if not m["is_read"] else " ")
        lines.append(
            f"  {flag} {m['age_hours']:>5.1f}h  {m['from'][:42]:42s}  {m['subject'][:60]}"
        )
    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


@tool(
    "email_search",
    "Substring search over the last 200 messages (subject + sender + "
    "body). Returns up to 20 matches with snippet. Use when Kunal "
    "asks 'find the email from X' or 'what did Y say about Z'.",
    {"query": str, "limit": int},
)
async def email_search_tool(args: dict) -> dict:
    q = (args.get("query") or "").strip()
    if not q:
        return {"content": [{"type": "text", "text": "email_search: query required"}]}
    limit = max(1, min(20, int(args.get("limit") or 10)))
    rows = await search_messages(q, limit=limit)
    if not rows:
        return {"content": [{"type": "text", "text": f"no matches for {q!r}"}]}
    lines = [f"{len(rows)} matches for {q!r}:"]
    for m in rows:
        lines.append(
            f"\n  {(m.get('sent_at','') or '')[:16]}  {m.get('direction','?'):8s}  "
            f"{m.get('from_address','')[:50]}"
        )
        lines.append(f"    subj: {(m.get('subject','') or '')[:120]}")
        snip = (m.get("snippet") or m.get("body_text") or "").strip()[:200]
        if snip:
            lines.append(f"    {snip}")
    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


@tool(
    "email_top_senders",
    "Who fills Kunal's inbox most over the last N days — the raw "
    "frequency table for the person-CRM. Excludes noreply/notification "
    "noise.",
    {"window_days": int, "limit": int},
)
async def email_top_senders_tool(args: dict) -> dict:
    days = max(1, min(180, int(args.get("window_days") or 30)))
    limit = max(5, min(50, int(args.get("limit") or 15)))
    rows = await top_senders_window(window_days=days, limit=limit)
    if not rows:
        return {"content": [{"type": "text",
                              "text": f"no senders in last {days}d"}]}
    lines = [f"top senders · last {days}d"]
    for r in rows:
        name = (r["names"][0] if r["names"] else "") or "(no name)"
        last = (r["last_sent"] or "")[:16]
        lines.append(
            f"  {r['count']:>3d}  {name[:30]:30s}  {r['email'][:40]:40s}  last {last}"
        )
    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


@tool(
    "email_classify_sweep",
    "Run Haiku over unclassified inbound messages and persist category "
    "+ priority + one-line summary + action_needed flag. Idempotent: "
    "safely re-runnable. Usually fires automatically at 12:40 IST; "
    "invoke directly when you just imported a batch or noticed "
    "the UI is full of 'unclassified' rows.",
    {"max_messages": int, "include_retries": bool},
)
async def email_classify_sweep_tool(args: dict) -> dict:
    from astra.email.classify import classify_sweep

    max_m = max(1, min(200, int(args.get("max_messages") or 50)))
    retries = bool(args.get("include_retries", True))
    r = await classify_sweep(
        max_messages=max_m, include_retries=retries,
    )
    text = (
        f"classifier sweep — scanned {r['scanned']}, "
        f"classified {r['classified']}, failed {r['failed']}"
    )
    return {"content": [{"type": "text", "text": text}]}


def create_email_mcp_server():
    return create_sdk_mcp_server(
        name="astra-email",
        version="0.1.0",
        tools=[
            email_digest_tool,
            email_unanswered_tool,
            email_search_tool,
            email_top_senders_tool,
            email_classify_sweep_tool,
        ],
    )
