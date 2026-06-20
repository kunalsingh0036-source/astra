"""
Reply-draft tools — the conversational surface for the inbox beachhead.

The inbox_triage job stages reply drafts in the email-agent (READY,
unsent). These tools let Astra DRIVE that staged work from chat —
WhatsApp or web — so Kunal can clear his replies by talking:

  "show my drafts"            → list_pending_replies
  "send the Rao one"          → send_reply_draft(<id>)
  "make the Ankur one shorter" → refine_reply_draft(<id>, "shorter")
  "drop the FHRAI draft"      → discard_reply_draft(<id>)
  "how's my draft rate"       → reply_draft_metrics

send_reply_draft is the only WRITE that puts a real email in the world,
and it fires ONLY when Kunal names a specific draft to send — that
instruction IS the per-send human approval. Nothing auto-sends.
"""

from __future__ import annotations

import httpx

from astra.email.client import BASE_URL, mesh_headers
from astra.runtime.sdk_compat import create_sdk_mcp_server, tool

_TIMEOUT = 30.0


def _short(s: str | None, n: int) -> str:
    s = (s or "").strip().replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


@tool(
    "list_pending_replies",
    "List the reply DRAFTS waiting for Kunal — replies Astra already "
    "wrote to action-needed mail that haven't been sent yet. Each row "
    "has an id (use it with send/refine/discard), who it's to, the "
    "subject, and a preview. Use when Kunal says 'show my drafts', "
    "'what replies are waiting', or after a triage nudge.",
    {"limit": int},
)
async def list_pending_replies_tool(args: dict) -> dict:
    limit = max(1, min(20, int(args.get("limit") or 10)))
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(
                f"{BASE_URL}/api/v1/drafts/",
                params={"status": "ready", "limit": limit},
                headers=mesh_headers(),
            )
        if r.status_code != 200:
            return _err(f"couldn't load drafts ({r.status_code}): {r.text[:160]}")
        rows = r.json() or []
    except Exception as e:
        return _err(f"drafts unavailable: {e}")

    if not rows:
        return _ok("No reply drafts waiting — inbox is clear of staged replies.")

    lines = [f"{len(rows)} reply draft(s) waiting:"]
    for i, d in enumerate(rows, 1):
        to = ", ".join(d.get("to_addresses") or []) or "(no recipient)"
        lines.append(
            f"\n[{i}] id={d['id']}\n"
            f"    to:   {_short(to, 60)}\n"
            f"    subj: {_short(d.get('subject'), 70)}\n"
            f"    {_short(d.get('body_text'), 220)}"
        )
    lines.append(
        "\nTo act: send_reply_draft(id), refine_reply_draft(id, instruction), "
        "or discard_reply_draft(id)."
    )
    return _ok("\n".join(lines))


@tool(
    "send_reply_draft",
    "SEND a specific reply draft as a real email via Gmail (in-thread). "
    "Call this ONLY when Kunal has named a specific draft to send — that "
    "is his approval to send it. Pass the draft id from "
    "list_pending_replies. Optionally pass edited_body to send a version "
    "Kunal revised verbatim. This WRITES — a real email goes out. After "
    "sending, the draft is marked sent and the original is marked read.",
    {"draft_id": str, "edited_body": str},
)
async def send_reply_draft_tool(args: dict) -> dict:
    draft_id = (args.get("draft_id") or "").strip()
    if not draft_id:
        return _err("send_reply_draft: draft_id required")
    body: dict = {}
    edited = (args.get("edited_body") or "").strip()
    if edited:
        body["body_override"] = edited
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.post(
                f"{BASE_URL}/api/v1/drafts/{draft_id}/send",
                json=body,
                headers=mesh_headers(),
            )
        if r.status_code != 200:
            return _err(f"send failed ({r.status_code}): {r.text[:200]}")
        data = r.json() or {}
    except Exception as e:
        return _err(f"send error: {e}")
    edited_note = " (your edited version)" if edited else ""
    return _ok(f"Sent{edited_note}. Gmail id {data.get('gmail_id', '?')}.")


@tool(
    "refine_reply_draft",
    "Revise a waiting reply draft per Kunal's instruction (e.g. "
    "'shorter', 'more formal', 'add that we ship in 2 weeks'). Keeps "
    "his voice. Pass the draft id and the instruction. Does NOT send — "
    "the revised draft stays waiting for approval.",
    {"draft_id": str, "instruction": str},
)
async def refine_reply_draft_tool(args: dict) -> dict:
    draft_id = (args.get("draft_id") or "").strip()
    instruction = (args.get("instruction") or "").strip()
    if not draft_id or not instruction:
        return _err("refine_reply_draft: draft_id and instruction required")
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.post(
                f"{BASE_URL}/api/v1/drafts/{draft_id}/refine",
                json={"instruction": instruction},
                headers=mesh_headers(),
            )
        if r.status_code != 200:
            return _err(f"refine failed ({r.status_code}): {r.text[:200]}")
        d = r.json() or {}
    except Exception as e:
        return _err(f"refine error: {e}")
    return _ok(
        f"Revised. New draft to {', '.join(d.get('to_addresses') or [])}:\n\n"
        f"Subject: {d.get('subject', '')}\n\n{d.get('body_text', '')}"
    )


@tool(
    "discard_reply_draft",
    "Discard a waiting reply draft Kunal doesn't want to send. Pass the "
    "draft id. The draft is marked discarded (counts against draft-sent "
    "rate as a 'no'). Use when he says 'drop it' / 'I'll handle that one'.",
    {"draft_id": str},
)
async def discard_reply_draft_tool(args: dict) -> dict:
    draft_id = (args.get("draft_id") or "").strip()
    if not draft_id:
        return _err("discard_reply_draft: draft_id required")
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.post(
                f"{BASE_URL}/api/v1/drafts/{draft_id}/discard",
                headers=mesh_headers(),
            )
        if r.status_code != 200:
            return _err(f"discard failed ({r.status_code}): {r.text[:160]}")
    except Exception as e:
        return _err(f"discard error: {e}")
    return _ok("Discarded.")


@tool(
    "reply_draft_metrics",
    "The inbox beachhead's value number: over the last N days, how many "
    "reply drafts were generated, sent (as-is vs edited), discarded, and "
    "still pending — plus draft-sent rate and estimated time saved. Use "
    "when Kunal asks how the draft system is doing, or for the Friday "
    "review.",
    {"days": int},
)
async def reply_draft_metrics_tool(args: dict) -> dict:
    days = max(1, min(90, int(args.get("days") or 7)))
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(
                f"{BASE_URL}/api/v1/drafts/metrics",
                params={"days": days},
                headers=mesh_headers(),
            )
        if r.status_code != 200:
            return _err(f"metrics failed ({r.status_code}): {r.text[:160]}")
        m = r.json() or {}
    except Exception as e:
        return _err(f"metrics error: {e}")
    rate = m.get("draft_sent_rate")
    rate_txt = f"{rate:.0%}" if isinstance(rate, (int, float)) else "n/a"
    mins = m.get("est_minutes_saved", 0)
    text = (
        f"Inbox drafts · last {m.get('window_days', days)}d\n"
        f"  generated: {m.get('generated', 0)}\n"
        f"  sent:      {m.get('sent', 0)}  "
        f"({m.get('sent_as_is', 0)} as-is, {m.get('sent_edited', 0)} edited)\n"
        f"  discarded: {m.get('discarded', 0)}\n"
        f"  pending:   {m.get('pending', 0)}\n"
        f"  draft-sent rate: {rate_txt}\n"
        f"  est. time saved: ~{mins} min ({mins // 60}h{mins % 60:02d})"
    )
    return _ok(text)


def _ok(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}


def _err(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "is_error": True}


def create_reply_mcp_server():
    return create_sdk_mcp_server(
        name="astra-replies",
        version="0.1.0",
        tools=[
            list_pending_replies_tool,
            send_reply_draft_tool,
            refine_reply_draft_tool,
            discard_reply_draft_tool,
            reply_draft_metrics_tool,
        ],
    )
