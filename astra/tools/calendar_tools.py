"""
MCP tools for Google Calendar — read access for Astra's briefings
and agent conversations.

Five tools:
  - calendar_status()              → is the Google OAuth set up?
  - calendar_today()               → today's events (IST day)
  - calendar_tomorrow()            → tomorrow's events (IST day)
  - calendar_week()                → next 7 days
  - calendar_search(query)         → substring match on summary / description

Write tools (create / update / delete) are deliberately not here yet.
They'll arrive behind the same approval-gated pattern that notes
writeback uses — stage a pending row, Kunal clicks Apply, a worker
performs the API call.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from astra.runtime.sdk_compat import create_sdk_mcp_server, tool

from astra.calendar.client import is_authorized
from astra.calendar.store import (
    list_events_between,
    list_events_today,
    list_events_tomorrow,
    search_events,
)


IST = timezone(timedelta(hours=5, minutes=30))


def _fmt_one(ev: dict) -> str:
    """One-line event render in IST — the briefing's preferred shape."""
    summary = ev.get("summary") or "(no title)"
    start = ev.get("start_at")
    end = ev.get("end_at")
    all_day = ev.get("is_all_day")

    if all_day:
        time_s = "all-day"
    elif start and end:
        try:
            s_ist = datetime.fromisoformat(start).astimezone(IST)
            e_ist = datetime.fromisoformat(end).astimezone(IST)
            time_s = f"{s_ist.strftime('%H:%M')}–{e_ist.strftime('%H:%M')}"
        except Exception:
            time_s = "?"
    elif start:
        try:
            time_s = (
                datetime.fromisoformat(start).astimezone(IST).strftime("%H:%M")
            )
        except Exception:
            time_s = "?"
    else:
        time_s = "?"

    extras: list[str] = []
    loc = ev.get("location")
    if loc:
        extras.append(loc[:60])
    meet = ev.get("meet_link")
    if meet:
        extras.append("[video]")
    attendees = ev.get("attendees") or []
    if attendees:
        emails = [a.get("email") for a in attendees if a.get("email")]
        extras.append(f"{len(emails)} att")

    extras_s = f" · {' · '.join(extras)}" if extras else ""
    return f"  {time_s}  {summary}{extras_s}"


def _render_list(header: str, events: list[dict]) -> str:
    if not events:
        return f"{header}\n  (nothing scheduled)"
    lines = [header]
    for e in events:
        lines.append(_fmt_one(e))
    return "\n".join(lines)


@tool(
    "calendar_status",
    "Returns whether Google Calendar is connected (OAuth consent "
    "completed). Call this before the other calendar tools if you're "
    "unsure — if unauthorized, tell Kunal to run the one-time consent.",
    {},
)
async def calendar_status_tool(_: dict) -> dict:
    ok = is_authorized()
    msg = (
        "Google Calendar: connected. Sync runs every 10 min."
        if ok
        else "Google Calendar: NOT connected. Run the OAuth consent flow "
        "to link Kunal's calendar."
    )
    return {"content": [{"type": "text", "text": msg}]}


@tool(
    "calendar_today",
    "List today's events on Kunal's primary calendar (IST calendar day). "
    "Use when he asks about his schedule today or you need to see "
    "what's already happened this morning/afternoon.",
    {},
)
async def calendar_today_tool(_: dict) -> dict:
    if not is_authorized():
        return _unauthorized()
    events = await list_events_today()
    text = _render_list(f"today · {len(events)} events", events)
    return {"content": [{"type": "text", "text": text}]}


@tool(
    "calendar_tomorrow",
    "List tomorrow's events (IST calendar day). Use this for planning "
    "briefings, confirming what's booked, or answering 'what's tomorrow?'",
    {},
)
async def calendar_tomorrow_tool(_: dict) -> dict:
    if not is_authorized():
        return _unauthorized()
    events = await list_events_tomorrow()
    text = _render_list(f"tomorrow · {len(events)} events", events)
    return {"content": [{"type": "text", "text": text}]}


@tool(
    "calendar_week",
    "List all events in the next 7 days. Use for a wider-horizon "
    "planning view than today/tomorrow.",
    {},
)
async def calendar_week_tool(_: dict) -> dict:
    if not is_authorized():
        return _unauthorized()
    now = datetime.now(timezone.utc)
    events = await list_events_between(now, now + timedelta(days=7))
    text = _render_list(f"next 7 days · {len(events)} events", events)
    return {"content": [{"type": "text", "text": text}]}


@tool(
    "calendar_search",
    "Substring search on summary / description / location across the "
    "next 30 days (configurable). Use when Kunal asks 'when was the "
    "call with X' or 'do I have a meeting about Y'.",
    {"query": str, "window_days": int},
)
async def calendar_search_tool(args: dict) -> dict:
    q = (args.get("query") or "").strip()
    if not q:
        return {"content": [{"type": "text", "text": "calendar_search: query required"}]}
    if not is_authorized():
        return _unauthorized()
    window = max(1, min(180, int(args.get("window_days") or 30)))
    events = await search_events(q, window_days=window)
    text = _render_list(f"matches for {q!r} · {len(events)} events", events)
    return {"content": [{"type": "text", "text": text}]}


def _unauthorized() -> dict:
    return {
        "content": [
            {
                "type": "text",
                "text": (
                    "Google Calendar is not connected. Kunal needs to "
                    "run the one-time OAuth consent flow — tell him to "
                    "run `python -c 'from astra.calendar.client import "
                    "get_calendar_service; get_calendar_service()'` "
                    "from the astra directory."
                ),
            }
        ]
    }


def create_calendar_mcp_server():
    return create_sdk_mcp_server(
        name="astra-calendar",
        version="0.1.0",
        tools=[
            calendar_status_tool,
            calendar_today_tool,
            calendar_tomorrow_tool,
            calendar_week_tool,
            calendar_search_tool,
        ],
    )
