"""Browser tools — Astra's hands in Kunal's real, logged-in Chrome.

Read tasks run unattended. Act tasks (click/type/navigate) are STAGED
and only execute once approved; that gate lives server-side in
astra/browser/store.py so a modified extension cannot bypass it.

Nothing here can submit a form, send a message, or complete a payment.
"""

from __future__ import annotations

import asyncio
import logging

from astra.runtime.sdk_compat import create_sdk_mcp_server, tool

logger = logging.getLogger(__name__)


def _ok(t: str) -> dict:
    return {"content": [{"type": "text", "text": t}]}


async def _await_result(task_id: str, timeout_s: int = 75) -> dict | None:
    """Wait for the extension to pick the task up and report back.

    The browser polls once a minute, so a cold wait can be ~60s. If it
    times out we say the browser was not available — never that the task
    returned nothing, because those are different facts.
    """
    from astra.browser.store import get_task

    for _ in range(timeout_s):
        await asyncio.sleep(1)
        t = await get_task(task_id)
        if t and t["status"] in ("done", "error", "expired"):
            return t
    return None


@tool(
    "browser_read",
    "Read what is on a page in Kunal's own logged-in Chrome — text, "
    "links, form fields and buttons, structured. Use for anything behind "
    "a login that has no API (LinkedIn, X, portals, dashboards, bank or "
    "GST sites). url_pattern picks the tab by URL substring; omit it to "
    "use the active tab. Requires the Astra Hands extension installed "
    "and Chrome open.",
    {"url_pattern": str},
)
async def browser_read_tool(args: dict) -> dict:
    from astra.browser.store import enqueue

    t = await enqueue(kind="read_page", url_pattern=(args.get("url_pattern") or "").strip())
    done = await _await_result(t["id"])
    if done is None:
        return _ok("Chrome did not pick that up in time. The browser may be closed or "
                   "the Astra Hands extension is not running. Nothing was read.")
    if done["status"] != "done":
        return _ok(f"Browser read failed: {done.get('error') or 'unknown error'}")
    r = done.get("result") or {}
    lines = [f"{r.get('title','(no title)')}  —  {r.get('url','')}", ""]
    lines.append((r.get("text") or "")[:4000])
    if r.get("fields"):
        lines.append("\nFields (index · what it is · current value):")
        for f in r["fields"][:15]:
            label = f.get("aria") or f.get("placeholder") or f.get("name") or f.get("type")
            lines.append(f"  {f['idx']} · {label} · {f.get('value','')[:40]}")
    if r.get("buttons"):
        lines.append("\nButtons: " + ", ".join(
            b["text"] for b in r["buttons"][:12] if b.get("text")))
    return _ok("\n".join(lines))


@tool(
    "browser_extract",
    "Pull specific elements from a page in Chrome by CSS selector. Use "
    "when browser_read gives too much and you know the shape of what you "
    "want (rows in a table, cards in a feed, line items in an invoice).",
    {"selector": str, "url_pattern": str, "limit": int},
)
async def browser_extract_tool(args: dict) -> dict:
    from astra.browser.store import enqueue

    sel = (args.get("selector") or "").strip()
    if not sel:
        return _ok("browser_extract: selector required")
    t = await enqueue(kind="extract",
                      url_pattern=(args.get("url_pattern") or "").strip(),
                      payload={"selector": sel, "limit": int(args.get("limit") or 30)})
    done = await _await_result(t["id"])
    if done is None:
        return _ok("Chrome did not pick that up in time (browser closed, or the "
                   "extension is not running). Nothing was extracted.")
    if done["status"] != "done":
        return _ok(f"Extract failed: {done.get('error') or 'unknown'}")
    items = (done.get("result") or {}).get("items") or []
    if not items:
        return _ok(f"No elements matched {sel!r} on that page.")
    out = [f"{len(items)} match(es) for {sel!r}:"]
    for i, it in enumerate(items[:25], 1):
        out.append(f"  {i}. {(it.get('text') or '')[:180]}"
                   + (f"  [{it['href']}]" if it.get("href") else ""))
    return _ok("\n".join(out))


@tool(
    "browser_stage_action",
    "STAGE an action in Chrome for Kunal to approve — a click, typing "
    "into a field, or navigating. It does NOT run until he approves it. "
    "Use after browser_read has shown what is on the page. There is no "
    "way to submit a form, send a message or pay from here.",
    {"action": str, "url_pattern": str, "text": str, "index": int, "value": str, "url": str},
)
async def browser_stage_action_tool(args: dict) -> dict:
    from astra.browser.store import enqueue

    action = (args.get("action") or "").strip().lower()
    if action not in ("click", "type", "navigate", "scroll"):
        return _ok("action must be one of: click, type, navigate, scroll")
    payload = {
        "text": args.get("text") or "", "index": int(args.get("index") or 0),
        "value": args.get("value") or "", "url": args.get("url") or "",
        "dy": 800,
    }
    t = await enqueue(kind=action, url_pattern=(args.get("url_pattern") or "").strip(),
                      payload=payload, approved=False)
    what = {"click": f"click {payload['text']!r}",
            "type": f"type into field {payload['index']}",
            "navigate": f"go to {payload['url']}",
            "scroll": "scroll"}[action]
    return _ok(
        f"Staged (NOT run): {what}.\nTask {t['id']}.\n"
        "Tell Kunal it is waiting on his approval — Astra will not act in his "
        "browser until he says so."
    )


def create_browser_mcp_server():
    return create_sdk_mcp_server(
        name="browser",
        tools=[browser_read_tool, browser_extract_tool, browser_stage_action_tool],
    )
