"""
WhatsApp → Astra chat channel.

The missing half of the router's promise ("Default → hold as pending,
notify Astra" — the notify never existed). When an inbound message
comes from one of Kunal's own numbers (ASTRA_OWNER_NUMBERS), it is
NOT routed to a business agent or held pending — it becomes a chat
turn against the Astra lean runtime, and the reply is sent back on
WhatsApp. This is what makes Astra textable.

Flow:
  webhook → _handle_inbound_message stores the message as usual →
  detects owner number → fire-and-forget chat_and_reply():
    1. POST  {STREAM}/turns/start  {prompt, session_id} (mesh secret)
    2. poll  {STREAM}/turns/{id}/result until terminal
    3. send the response text back via MetaAPIClient.send_text
       (direct send, NOT the queue: CooldownService would reject a
       second reply within 24h — cooldowns exist to stop outreach
       spam, not to rate-limit Kunal talking to his own agent)

Session continuity: session_id is a UUIDv5 of the phone number, so
every WhatsApp exchange from the same number lands in one continuous
Astra session (rehydrated server-side like any web session).

Safety rails: owner-numbers allowlist only (everyone else keeps the
normal routing/hold behaviour); text messages only in v1; prompt
capped at 4k chars; turn polling bounded to ~260s (past the runner's
240s hard cap); WhatsApp text replies chunked to Meta's 4096-char
limit.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid

import httpx

logger = logging.getLogger(__name__)

# Namespace for deriving stable session ids from phone numbers.
_SESSION_NS = uuid.UUID("a57a0000-77a7-4c8a-9d3e-000000000001")

_STREAM_URL = os.environ.get(
    "STREAM_URL", "http://stream.railway.internal:8080"
).rstrip("/")

_MAX_PROMPT_CHARS = 4_000
_POLL_INTERVAL_S = 2.0
_POLL_BUDGET_S = 260.0  # runner hard cap is 240s; margin for finalize
_WA_TEXT_LIMIT = 4_096


def owner_numbers() -> set[str]:
    """Phone numbers (normalized, digits with country code) whose
    messages route to Astra chat instead of business agents."""
    raw = os.environ.get("ASTRA_OWNER_NUMBERS", "")
    return {n.strip().lstrip("+") for n in raw.split(",") if n.strip()}


def is_owner(phone: str) -> bool:
    return phone.lstrip("+") in owner_numbers()


def _mesh_headers() -> dict[str, str]:
    return {
        "x-astra-secret": os.environ.get("AGENT_SHARED_SECRET", "").strip()
    }


def session_id_for(phone: str) -> str:
    """Per-day session key: uuid5(phone + IST date).

    The original key was uuid5(phone) alone — ONE ever-growing session
    for all WhatsApp history. First live use surfaced the failure mode:
    rehydrating months of accumulated turns let a stale test question's
    framing bleed into a fresh answer (turn 319 replied to a two-turns-
    old prompt). Day-scoped keys match how texting an assistant
    actually feels — "today's conversation" — and keep every session's
    rehydration bounded. Cross-day continuity is the memory system's
    job (post-turn extraction + recall_memories), not rehydration's.

    IST day boundary, not UTC: Kunal's day is the natural session
    boundary, and his midnight is 18:30 UTC.
    """
    from datetime import datetime, timedelta, timezone

    ist_today = (
        datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    ).date()
    return str(uuid.uuid5(_SESSION_NS, f"{phone.lstrip('+')}:{ist_today}"))


async def _reply_text(phone: str, text: str) -> None:
    """Send a plain text reply to the owner on WhatsApp, chunked to
    Meta's limit. Never raises. Used for canned replies (e.g. a voice
    note we couldn't transcribe) that don't need an Astra turn."""
    from gateway.services.meta_api import MetaAPIClient

    client = MetaAPIClient()
    try:
        for chunk in _chunks(text, _WA_TEXT_LIMIT):
            result = await client.send_text(phone=phone, body=chunk)
            if not result.success:
                logger.error("[astra-chat] reply send failed: %s", result.error)
                break
    except Exception:
        logger.exception("[astra-chat] _reply_text raised")
    finally:
        await client.close()


async def chat_and_reply(phone: str, text: str) -> None:
    """Run one Astra turn for an owner message and reply on WhatsApp.

    Never raises — webhook background tasks must not explode. Every
    failure path sends SOMETHING back so Kunal isn't left on read.
    """
    reply = await _run_turn(phone, text)
    await _reply_text(phone, reply)


async def _run_turn(phone: str, text: str) -> str:
    prompt = text.strip()[:_MAX_PROMPT_CHARS]
    if not prompt:
        return "(empty message — text me words!)"

    body = {
        "prompt": prompt,
        "session_id": session_id_for(phone),
        "channel": "whatsapp",
    }
    try:
        async with httpx.AsyncClient(timeout=20.0) as c:
            r = await c.post(
                f"{_STREAM_URL}/turns/start",
                json=body,
                headers=_mesh_headers(),
            )
        if r.status_code != 200:
            logger.error(
                "[astra-chat] turns/start → %s: %s",
                r.status_code,
                r.text[:200],
            )
            return (
                "couldn't start a turn (stream service said "
                f"{r.status_code}). try again in a minute."
            )
        turn_id = (r.json() or {}).get("turn_id")
        if not turn_id:
            return "stream service didn't return a turn id — try again."
    except Exception as e:
        logger.error("[astra-chat] turns/start error: %s", e)
        return "couldn't reach the brain (stream service). try again shortly."

    # Poll for the result. The /turns/{id}/result endpoint reads the
    # turns row — terminal when status leaves 'running'.
    deadline = asyncio.get_event_loop().time() + _POLL_BUDGET_S
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            while asyncio.get_event_loop().time() < deadline:
                rr = await c.get(
                    f"{_STREAM_URL}/turns/{turn_id}/result",
                    headers=_mesh_headers(),
                )
                if rr.status_code == 200:
                    data = rr.json() or {}
                    if data.get("terminal"):
                        if data.get("status") == "complete":
                            resp = (data.get("response") or "").strip()
                            return resp or "(done — no text response)"
                        err = data.get("error_message") or data.get("status")
                        return f"turn ended without an answer ({err}). retry?"
                await asyncio.sleep(_POLL_INTERVAL_S)
    except Exception as e:
        logger.error("[astra-chat] poll error: %s", e)
        return "lost the connection mid-turn. ask again?"

    return (
        "still working after 4+ minutes — that turn hit the time cap. "
        "try a narrower ask."
    )


def _chunks(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    out = []
    while text:
        out.append(text[:limit])
        text = text[limit:]
    return out
