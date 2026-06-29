"""
"Kunal Now" — a compact, verified-fresh snapshot of what's happening in
Kunal's life RIGHT NOW, injected into every chat turn's system prompt.

Why this exists: Astra has rich context (calendar, inbox, training, memory)
but recall is PULL-only — the agent gets it only if it decides to call a
tool. So a question like "what's on me today?" can be answered blind. This
PUSHES a tight, live block into every turn so every response is grounded,
without the model having to remember to look.

Design rules (hard-won this session):
- COMPACT: a handful of lines (~250–400 tokens), not the briefing dump.
- VERIFIED-FRESH: every line is current or explicitly marked STALE /
  DISCONNECTED (the Gmail-blackout lesson — never present frozen data as
  live).
- NON-BLOCKING: each source has its own timeout + try/except; a slow or
  dead source drops its line, never stalls the turn.
- TTL-CACHED (~90s): a multi-message conversation rebuilds at most every
  90s — also keeps the system prompt stable enough for prompt caching.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))
_TTL_SECONDS = 90.0
_SRC_TIMEOUT = 5.0
_cache: dict[str, object] = {"block": None, "ts": 0.0}


async def build_kunal_now() -> str:
    """Return the <kunal_now> block, or "" if everything failed (never
    inject a broken/empty block). Cached for _TTL_SECONDS."""
    mono = time.monotonic()
    cached = _cache.get("block")
    if isinstance(cached, str) and (mono - float(_cache["ts"])) < _TTL_SECONDS:
        return cached

    results = await asyncio.gather(
        _guard(_calendar_line()),
        _guard(_inbox_line()),
        _guard(_training_line()),
        _guard(_focus_lines()),
        return_exceptions=False,
    )
    lines = [r for r in results if r]
    if not lines:
        return ""

    ist = datetime.now(_IST)
    block = (
        f'<kunal_now note="Live context for Kunal as of '
        f'{ist:%a %d %b %H:%M} IST. Ground your answer in this. Lines '
        f'marked STALE/DISCONNECTED are NOT current — say so if relevant.">\n'
        + "\n".join(lines)
        + "\n</kunal_now>"
    )
    _cache["block"] = block
    _cache["ts"] = mono
    return block


async def _guard(coro) -> str:
    """Run a source with a timeout; any failure → "" (drop the line)."""
    try:
        return await asyncio.wait_for(coro, timeout=_SRC_TIMEOUT)
    except Exception as e:
        logger.info("[kunal_now] source dropped: %s", e)
        return ""


async def _calendar_line() -> str:
    from astra.calendar.store import list_events_between

    now = datetime.now(_IST)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=2)
    rows = await list_events_between(start.astimezone(timezone.utc), end.astimezone(timezone.utc), limit=40)
    if not rows:
        return "📅 Calendar: clear today + tomorrow."

    def fmt(evs: list) -> str:
        out = []
        for e in evs[:4]:
            sa = e.get("start_at")
            t = sa.astimezone(_IST).strftime("%H:%M") if hasattr(sa, "astimezone") else ""
            out.append(f"{t} {(e.get('summary') or 'untitled')[:32]}".strip())
        return "; ".join(out) if out else "—"

    today_d, tmrw_d = now.date(), (now + timedelta(days=1)).date()
    today, tmrw = [], []
    for e in rows:
        sa = e.get("start_at")
        if not hasattr(sa, "astimezone"):
            continue
        d = sa.astimezone(_IST).date()
        (today if d == today_d else tmrw if d == tmrw_d else []).append(e)
    seg = []
    seg.append(f"today: {fmt(today)}" if today else "today: clear")
    if tmrw:
        seg.append(f"tomorrow: {fmt(tmrw)}")
    return "📅 Calendar — " + " · ".join(seg)


async def _inbox_line() -> str:
    from astra.email.client import BASE_URL, get_summary, mesh_headers

    # Gmail liveness first — a dead token freezes the counts (the blackout
    # lesson). If auth is down, say DISCONNECTED, don't quote stale numbers.
    gmail_ok = True
    try:
        import httpx

        async with httpx.AsyncClient(timeout=4.0) as c:
            r = await c.get(f"{BASE_URL}/health/gmail", headers=mesh_headers())
            gmail_ok = r.status_code == 200
    except Exception:
        gmail_ok = None  # unknown — don't cry wolf

    if gmail_ok is False:
        return "📧 Inbox — 🔴 EMAIL DISCONNECTED (Gmail auth down; not syncing). Run scripts/gmail_reauth.py."

    summary = await get_summary()
    if not summary:
        return ""
    unread = summary.get("unread", 0)
    action = summary.get("action_needed", 0)
    suffix = "" if gmail_ok else " (liveness unverified)"
    return f"📧 Inbox — {unread} unread, {action} action-needed{suffix}."


async def _training_line() -> str:
    from astra.notes.missed_sessions import trend

    tr = await trend(14)
    if not tr or tr.get("today") is None:
        return "🏋 Training — log STALE (no recent entry; needs the Mac/bridge)."
    direction = tr.get("direction") or {}
    # surface only the notable movers (closing/growing), keep it terse
    closing = [k for k, v in direction.items() if "closed" in str(v)]
    growing = [k for k, v in direction.items() if "grew" in str(v)]
    bits = []
    if closing:
        bits.append("recovering: " + ", ".join(closing[:3]))
    if growing:
        bits.append("slipping: " + ", ".join(growing[:3]))
    return "🏋 Training — " + ("; ".join(bits) if bits else "flat week-over-week") + "."


async def _focus_lines() -> str:
    from sqlalchemy import text

    from astra.db.engine import async_session

    async with async_session() as s:
        r = await s.execute(
            text(
                """
                SELECT content FROM memories
                WHERE importance >= 0.7
                  AND created_at >= now() - interval '21 days'
                  AND (tags LIKE '%decision%' OR tags LIKE '%project%'
                       OR tags LIKE '%preference%' OR tags LIKE '%rule%'
                       OR tags LIKE '%task%')
                ORDER BY created_at DESC
                LIMIT 4
                """
            )
        )
        rows = [row[0] for row in r.all()]
    if not rows:
        return ""
    bullets = "; ".join((c or "").strip()[:120] for c in rows)
    return "🧠 Recent (decisions/focus) — " + bullets
