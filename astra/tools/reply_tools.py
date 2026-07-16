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


@tool(
    "learn_my_voice",
    "Run the voice-feedback loop now: distill how Kunal edited his email "
    "drafts before sending into voice-correction notes the drafter applies "
    "going forward, and show what was learned. Use when Kunal asks to "
    "update/refresh how Astra writes in his voice, or to see the current "
    "learned voice profile. No-op (reports it) until there are enough "
    "edited samples to learn from.",
    {},
)
async def learn_my_voice_tool(args: dict) -> dict:
    try:
        async with httpx.AsyncClient(timeout=120.0) as c:
            r = await c.post(
                f"{BASE_URL}/api/v1/ai/learn-voice",
                headers=mesh_headers(),
            )
        if r.status_code != 200:
            return _err(f"learn-voice failed ({r.status_code}): {r.text[:200]}")
        d = r.json() or {}
    except Exception as e:
        return _err(f"learn-voice error: {e}")
    if d.get("ok") is False and d.get("error"):
        return _err(f"voice-learn failed: {d.get('error')}")
    if not d.get("learned"):
        return _ok(
            "Not enough signal yet — " + (d.get("reason") or "no edited drafts") +
            ". The loop activates as you edit drafts before sending; each edit "
            "becomes training for how Astra writes as you."
        )
    return _ok(
        f"Learned your voice from {d.get('samples')} edited drafts. "
        f"The drafter now applies:\n\n{d.get('notes', '').strip()}"
    )


@tool(
    "ingest_voice_export",
    "Ingest a WhatsApp chat export (.txt) or Instagram DM export (JSON) "
    "into Kunal's voice corpus so the miner learns his TEXTING voice. "
    "channel: whatsapp_personal | instagram. Provide path (file on his "
    "Mac — read via the bridge) OR raw text for small pastes. self_name "
    "= his display name exactly as it appears in the export (WhatsApp: "
    "his profile name; Instagram: his account display name). Keeps ONLY "
    "his own messages; deduped, re-runnable. After ingesting exports, "
    "run learn_my_voice or wait for the weekly re-mine.",
    {"channel": str, "path": str, "text": str, "self_name": str},
)
async def ingest_voice_export_tool(args: dict) -> dict:
    channel = (args.get("channel") or "").strip().lower()
    self_name = (args.get("self_name") or "").strip()
    path = (args.get("path") or "").strip()
    raw = (args.get("text") or "").strip()
    if channel not in ("whatsapp_personal", "instagram"):
        return _err("channel must be whatsapp_personal or instagram")
    if not self_name:
        return _err("self_name required — Kunal's display name exactly as "
                    "it appears inside the export file")
    if not path and not raw:
        return _err("provide path (file on the Mac) or text (pasted export)")

    if path:
        # Path wins if both given — the file is the real export,
        # a paste is usually a small sample. Read via the Mac bridge in 2000-line pages. The
        # content stays inside this tool — it never enters chat context.
        import re as _re

        from astra.runtime.tools.local import _dispatch

        parts: list[str] = []
        offset, total = 1, None
        for _ in range(200):  # hard cap ~400k lines
            res = await _dispatch(
                "local_read", {"path": path, "offset": offset, "limit": 2000},
                timeout_sec=30.0,
            )
            txt = (res.get("content") or [{}])[0].get("text", "")
            if res.get("is_error"):
                return _err(txt)  # incl. the contextual bridge-offline message
            m = _re.match(r"# .*? \((\d+) lines, showing (\d+)[–-](\d+)\)\n?", txt)
            if not m:
                parts.append(txt)
                break
            total, end = int(m.group(1)), int(m.group(3))
            parts.append(txt[m.end():])
            if end >= total:
                break
            offset = end + 1
        raw = "".join(parts)
        if not raw.strip():
            return _err(f"file at {path} read empty — check the path")

    fmt = "instagram_json" if channel == "instagram" else "whatsapp_txt"
    try:
        async with httpx.AsyncClient(timeout=120.0) as c:
            r = await c.post(
                f"{BASE_URL}/api/v1/voice/corpus",
                json={"channel": channel, "format": fmt, "content": raw,
                      "self_name": self_name},
                headers=mesh_headers(),
            )
        if r.status_code != 200:
            return _err(f"ingest failed ({r.status_code}): {r.text[:200]}")
        d = r.json() or {}
    except Exception as e:
        return _err(f"ingest error: {e}")
    if not d.get("ok"):
        return _err(f"ingest failed: {d.get('error')}")
    return _ok(
        f"Ingested {channel}: parsed {d.get('parsed')} of Kunal's messages, "
        f"{d.get('new')} new ({d.get('duplicates')} already known). "
        f"Channel corpus now {d.get('channel_total')} messages. "
        f"Say “mine my voice” to rebuild the profiles now (needs ≥20 "
        f"messages per channel), or the Saturday job will."
    )


@tool(
    "draft_personal_reply",
    "Draft a reply IN KUNAL'S OWN TEXTING VOICE to a message someone sent "
    "him on a personal channel (WhatsApp/Instagram). Use when he pastes or "
    "forwards a message and wants a reply. Returns copy-paste-ready text — "
    "Astra CANNOT send from his personal accounts and must never claim to; "
    "he pastes it himself. channel: whatsapp | instagram. instruction = "
    "optional steer ('decline politely', 'say yes for Sunday').",
    {"channel": str, "their_message": str, "contact": str, "instruction": str},
)
async def draft_personal_reply_tool(args: dict) -> dict:
    msg = (args.get("their_message") or "").strip()
    if not msg:
        return _err("their_message required — paste what they sent")
    try:
        async with httpx.AsyncClient(timeout=90.0) as c:
            r = await c.post(
                f"{BASE_URL}/api/v1/voice/draft-reply",
                json={"channel": (args.get("channel") or "whatsapp").strip(),
                      "their_message": msg[:4000],
                      "contact": (args.get("contact") or "")[:120],
                      "instruction": (args.get("instruction") or "")[:400]},
                headers=mesh_headers(),
            )
        if r.status_code != 200:
            return _err(f"draft failed ({r.status_code}): {r.text[:200]}")
        d = r.json() or {}
    except Exception as e:
        return _err(f"draft error: {e}")
    if not d.get("ok"):
        return _err(f"draft failed: {d.get('error')}")
    return _ok(
        f"Ready to paste (voice: {d.get('register_used')}):\n\n{d.get('reply')}"
    )


@tool(
    "mine_my_voice",
    "Rebuild Kunal's voice profiles NOW from his real writing — his sent "
    "email registers AND his texting registers (whatsapp_personal / "
    "instagram) distilled from ingested chat exports. Use after ingesting "
    "exports, or when he says 'mine my voice' / 'rebuild my voice'. Runs "
    "in the background; reports what got mined.",
    {},
)
async def mine_my_voice_tool(args: dict) -> dict:
    try:
        async with httpx.AsyncClient(timeout=30.0) as c:
            r = await c.post(
                f"{BASE_URL}/api/v1/voice/mine", headers=mesh_headers()
            )
        if r.status_code != 200:
            return _err(f"mine failed ({r.status_code}): {r.text[:160]}")
        d = r.json() or {}
    except Exception as e:
        return _err(f"mine error: {e}")
    if d.get("running"):
        return _ok("Mining started — rebuilding your email + texting voice "
                   "profiles now. Ask 'show my voice profiles' in a minute.")
    res = d.get("result") or {}
    if res.get("mined"):
        return _ok("Re-mined. Registers: "
                   + ", ".join(f"{k} ({v})" for k, v in
                               (res.get("registers") or {}).items())
                   + ". Say 'show my voice profiles' to see them.")
    return _ok("Mining finished — " + (res.get("reason") or res.get("error")
               or "nothing new to mine (need ≥20 messages per channel)."))


@tool(
    "voice_profiles",
    "Show what Astra has learned of Kunal's voice: mined registers (email "
    "+ texting channels), sample counts, corpus sizes, last update. Use "
    "when he asks what you know about how he writes.",
    {},
)
async def voice_profiles_tool(args: dict) -> dict:
    regs = ["general", "business", "vendor_support", "personal",
            "formal_official", "whatsapp_personal", "instagram"]
    lines = ["Voice profiles (mined from Kunal's real writing):"]
    try:
        async with httpx.AsyncClient(timeout=30.0) as c:
            for reg in regs:
                r = await c.get(
                    f"{BASE_URL}/api/v1/voice/profile",
                    params={"register": reg}, headers=mesh_headers(),
                )
                d = r.json() if r.status_code == 200 else {}
                if (d.get("profile") or "").strip():
                    n = d.get("sample_count", "?")
                    ts = (d.get("updated_at") or "")[:10]
                    lines.append(f"  • {reg}: {n} samples, updated {ts}")
            r = await c.get(f"{BASE_URL}/api/v1/voice/corpus",
                            headers=mesh_headers())
            counts = (r.json() or {}).get("counts") or {}
            if counts:
                lines.append("Corpus: " + ", ".join(
                    f"{k} {v} msgs" for k, v in counts.items()))
    except Exception as e:
        return _err(f"voice status error: {e}")
    if len(lines) == 1:
        lines.append("  (nothing mined yet — ingest exports, then learn)")
    return _ok("\n".join(lines))


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
            learn_my_voice_tool,
            ingest_voice_export_tool,
            draft_personal_reply_tool,
            mine_my_voice_tool,
            voice_profiles_tool,
        ],
    )
