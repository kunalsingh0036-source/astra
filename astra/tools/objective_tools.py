"""Objective tools — Kunal creates and closes goals from chat/WhatsApp."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from astra.runtime.sdk_compat import create_sdk_mcp_server, tool

logger = logging.getLogger(__name__)


def _ok(t: str) -> dict:
    return {"content": [{"type": "text", "text": t}]}


@tool(
    "create_objective",
    "Create a self-pursuing GOAL that Astra chases until it resolves — "
    "not a task, not a reminder. Use when Kunal wants something followed "
    "up on repeatedly ('chase Samarth for the term sheet', 'keep after "
    "the glass vendor'). Astra re-checks on a cadence, DRAFTS each nudge "
    "for Kunal to send, escalates when it drags, and stops the moment the "
    "outcome lands. done_when='email_reply' auto-closes when that person "
    "replies; 'manual' means only Kunal can close it.",
    {"title": str, "goal": str, "done_when": str, "target_ref": str,
     "channel": str, "cadence_days": int, "deadline_days": int},
)
async def create_objective_tool(args: dict) -> dict:
    from astra.objectives.store import create_objective

    title = (args.get("title") or "").strip()
    if not title:
        return _ok("create_objective: title required")
    kind = (args.get("done_when") or "manual").strip().lower()
    if kind not in ("manual", "email_reply", "obligation_filed"):
        kind = "manual"
    target = (args.get("target_ref") or "").strip()
    if kind == "email_reply" and "@" not in target:
        return _ok("done_when='email_reply' needs target_ref to be an email address.")

    dl_days = int(args.get("deadline_days") or 0)
    obj = await create_objective(
        title=title,
        goal=(args.get("goal") or title).strip(),
        done_when={"kind": kind, "from": target} if kind == "email_reply" else {"kind": kind},
        channel=(args.get("channel") or ("email" if "@" in target else "none")).strip(),
        target_ref=target,
        cadence_days=max(1, int(args.get("cadence_days") or 3)),
        deadline_at=(datetime.now(timezone.utc) + timedelta(days=dl_days)) if dl_days else None,
    )
    closes = ("closes itself when they reply" if kind == "email_reply"
              else "you close it when it's done")
    return _ok(
        f"Objective #{obj['id']} created: {obj['title']}\n"
        f"Checks every {max(1, int(args.get('cadence_days') or 3))} days, {closes}. "
        f"Every nudge is DRAFTED for you — Astra never sends from your accounts."
    )


@tool(
    "list_objectives",
    "Show what Astra is currently chasing, with attempts made and when "
    "each is next checked. Use for 'what are you following up on'.",
    {"state": str},
)
async def list_objectives_tool(args: dict) -> dict:
    from astra.objectives.store import list_objectives

    state = (args.get("state") or "").strip() or None
    rows = await list_objectives(state=state)
    if not rows:
        return _ok("Nothing being chased right now.")
    lines = [f"{len(rows)} objective(s):"]
    for r in rows:
        nxt = r["next_check_at"].strftime("%d %b") if r.get("next_check_at") else "?"
        lines.append(
            f"  #{r['id']} [{r['state']}] {r['title']} — "
            f"{r['attempts']}/{r['max_attempts']} attempts, next {nxt}"
            + (f" · {r['close_reason']}" if r.get("close_reason") else "")
        )
    return _ok("\n".join(lines))


@tool(
    "close_objective",
    "Close a goal Astra is chasing — done, or drop it. Use when Kunal "
    "says 'got it', 'he replied', 'drop that one'.",
    {"objective_id": int, "outcome": str, "note": str},
)
async def close_objective_tool(args: dict) -> dict:
    from astra.objectives.store import close_objective, record_event

    oid = int(args.get("objective_id") or 0)
    if not oid:
        return _ok("close_objective: objective_id required")
    outcome = (args.get("outcome") or "done").strip().lower()
    state = "done" if outcome in ("done", "resolved", "got it") else "abandoned"
    reason = (args.get("note") or f"closed by Kunal ({outcome})").strip()
    await close_objective(oid, state=state, reason=reason)
    await record_event(oid, "closed", reason)
    return _ok(f"Objective #{oid} closed as {state}. No further chasing.")


def create_objectives_mcp_server():
    return create_sdk_mcp_server(
        name="objectives",
        tools=[create_objective_tool, list_objectives_tool, close_objective_tool],
    )
