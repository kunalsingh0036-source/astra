"""
Briefings v2 — decision documents, not stats dumps.

The compass's own success bar: "the chat is the LAST resort for
'what should I do today' — the briefing should answer it." v1
briefings reported fleet counts and memory stats (and, until the
2026-06-11 topology fixes, fictional ones). v2 assembles the real
signal — calendar, triaged inbox + staged drafts, honest fleet
health, training debt, research intel — frames it against the
compass (business-kits/compass.md), and has Claude write the brief
a chief-of-staff would hand Kunal.

Every section degrades independently: a dead source becomes one
honest clause ("calendar not connected"), never a crash and never
fiction. Delivery is layered: episodic memory (always) → web push
(always attempted) → WhatsApp via the gateway's owner-notify
endpoint (best effort; needs ASTRA_OWNER_NUMBERS + an open Meta
session window).
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import text as _sql

logger = logging.getLogger(__name__)

_IST_OFFSET = timedelta(hours=5, minutes=30)

# Topics Astra has NO data source for. The synthesis model has invented
# warnings about these (e.g. "iCloud storage is full", "a HelmTech
# project is hitting its Vercel limit") and presented them as measured
# watch-outs. If the brief mentions one of these and it is NOT in the
# DATA block, the output is fabricated — reject it. (2026-06-13 audit.)
_UNMONITORED = (
    "icloud",
    "vercel",
    "disk space",
    "storage is full",
    "storage full",
    "storage quota",
)


def _now_ist() -> datetime:
    return datetime.now(timezone.utc) + _IST_OFFSET


def _compass_text() -> str:
    """business-kits/compass.md — repo file, reviewed by Kunal.
    Replaces the dead laptop-path read v1 used."""
    base = os.environ.get("BUSINESS_KITS_DIR", "").strip()
    candidates = [
        Path(base) / "compass.md" if base else None,
        Path(__file__).resolve().parents[2] / "business-kits" / "compass.md",
    ]
    for p in candidates:
        if p and p.is_file():
            return p.read_text()[:4000]
    return "(compass file missing)"


async def _calendar_today() -> str:
    """Today's events (IST day window) from calendar_events."""
    from astra.db.engine import async_session

    ist_now = _now_ist()
    day_start = ist_now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = day_start - _IST_OFFSET
    end_utc = start_utc + timedelta(days=1)
    try:
        async with async_session() as s:
            r = await s.execute(
                _sql(
                    """
                    SELECT summary, start_at, end_at, is_all_day, meet_link
                    FROM calendar_events
                    WHERE status != 'cancelled'
                      AND start_at >= :a AND start_at < :b
                    ORDER BY start_at
                    LIMIT 12
                    """
                ),
                {"a": start_utc, "b": end_utc},
            )
            rows = r.fetchall()
        if not rows:
            # Distinguish "empty day" from "never synced".
            async with async_session() as s:
                r2 = await s.execute(_sql("SELECT count(*) FROM calendar_events"))
                total = r2.scalar() or 0
            return (
                "no events today" if total else "calendar not connected yet"
            )
        out = []
        for summary, start_at, end_at, all_day, meet in rows:
            if all_day:
                out.append(f"- all-day: {summary}")
            else:
                t = (start_at + _IST_OFFSET).strftime("%H:%M")
                out.append(f"- {t} {summary}" + (" (Meet)" if meet else ""))
        return "\n".join(out)
    except Exception as e:
        logger.warning("[briefing] calendar read failed: %s", e)
        return "calendar unavailable"


async def _inbox_state() -> tuple[str, dict]:
    """Counts + action-needed heads + staged draft count via the email
    agent (mesh HTTP). Returns (text, facts) — facts carry the exact
    numbers so the synthesised brief can be reconciled against them.
    The brief used to hallucinate "41 action-needed / 10 drafts staged"
    from a correct "9 / 0" (2026-06-13 audit)."""
    try:
        from astra.email.client import get_summary, list_messages, mesh_headers, BASE_URL

        summary = await get_summary()
        if not summary:
            return "email agent unreachable", {}
        total = summary.get("total", 0)
        unread = summary.get("unread", 0)
        action = summary.get("action_needed", 0)

        heads: list[str] = []
        if action:
            msgs = await list_messages(
                direction="inbound", action_needed_only=True, limit=5
            )
            for m in msgs[:5]:
                heads.append(
                    f"  · {m.get('from_address','?')} — {(m.get('subject') or '')[:60]}"
                )

        # Always state the draft count explicitly — including ZERO — so
        # the truth is positively asserted in the DATA, never just absent.
        # Absence is exactly what let the model invent "10 drafts staged."
        drafts_ready: int | None = 0
        try:
            async with httpx.AsyncClient(timeout=8.0) as c:
                r = await c.get(
                    f"{BASE_URL}/api/v1/drafts/", headers=mesh_headers()
                )
                if r.status_code == 200:
                    drafts_ready = len(
                        [d for d in (r.json() or []) if d.get("status") == "ready"]
                    )
                else:
                    drafts_ready = None
        except Exception:
            drafts_ready = None

        if drafts_ready is None:
            drafts_line = "\ndraft status unavailable"
        elif drafts_ready == 0:
            drafts_line = "\n0 reply drafts staged"
        else:
            drafts_line = (
                f"\n{drafts_ready} reply draft(s) staged and waiting "
                "for review on /email"
            )

        lines = [f"{total} synced · {unread} unread · {action} action-needed"]
        lines += heads
        facts = {
            "action_needed": int(action),
            "unread": int(unread),
            "total": int(total),
            "drafts_ready": drafts_ready,
        }
        return "\n".join(lines) + drafts_line, facts
    except Exception as e:
        logger.warning("[briefing] inbox read failed: %s", e)
        return "email agent unavailable", {}


async def _fleet_line() -> tuple[str, dict]:
    try:
        from astra.scheduler.jobs import _cloud_fleet_probe

        results = await _cloud_fleet_probe()
        bad = [r["service"] for r in results if r["status"] != "healthy"]
        facts = {"fleet_down": bad, "fleet_total": len(results)}
        if bad:
            return (
                f"{len(results)-len(bad)}/{len(results)} healthy — DOWN: {', '.join(bad)}",
                facts,
            )
        return f"all {len(results)} services healthy", facts
    except Exception:
        return "fleet probe unavailable", {}


async def _training_state() -> tuple[str, dict]:
    """Missed-session debt framed as the WEEK-OVER-WEEK trend, not the
    raw absolute.

    The counters are CUMULATIVE-since-inception (Kunal's Apple Note),
    so the absolute looks huge (×320) and screams false urgency. The
    real decision signal is direction: a counter falling = recovery on
    schedule; rising = slipping (compass rule, 2026-04-19). So we lead
    with the Δ/week per type and keep the absolute as a quiet suffix."""
    try:
        from astra.notes.missed_sessions import trend as _trend, TYPES

        tr = await _trend(14)
        today = tr.get("today")
        if not today:
            return "no training snapshot yet", {}

        wow = tr.get("wow_delta") or {}
        direction = tr.get("direction") or {}
        owed = {t: int(today[t]) for t in TYPES if (today.get(t) or 0) > 0}
        if not owed:
            return "no missed sessions", {"training_debts": {}}

        parts: list[str] = []
        for t, n in owed.items():
            d = wow.get(t)
            if d is None:
                parts.append(f"{t} {n}")
            else:
                arrow = "↓" if d < 0 else ("↑" if d > 0 else "→")
                tag = direction.get(t, "")
                parts.append(f"{t} {n} ({arrow}{abs(d)}/wk{(' ' + tag) if tag else ''})")
        line = "missed-session debt (week trend): " + ", ".join(parts)
        return line, {
            "training_debts": owed,
            "training_wow": {t: wow.get(t) for t in owed},
        }
    except Exception as e:
        logger.warning("[briefing] training read failed: %s", e)
        return "training data unavailable", {}


async def _research_line() -> str:
    from astra.db.engine import async_session

    try:
        since = datetime.now(timezone.utc) - timedelta(hours=14)
        async with async_session() as s:
            r = await s.execute(
                _sql(
                    """
                    SELECT topic, status, body_md FROM research_briefings
                    WHERE created_at >= :since
                    ORDER BY created_at DESC LIMIT 1
                    """
                ),
                {"since": since},
            )
            row = r.first()
        if not row or row[1] != "ready":
            return ""
        from astra.scheduler.jobs import _extract_gist_and_top

        return f"Research — {row[0]}: {_extract_gist_and_top(row[2])[:300]}"
    except Exception:
        return ""


async def _recent_turn_topics(hours: int = 18) -> str:
    """What Kunal and Astra actually worked on — session titles of
    turns in the window. Feeds the evening 'what we did' section."""
    from astra.db.engine import async_session

    try:
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
        async with async_session() as s:
            r = await s.execute(
                _sql(
                    """
                    SELECT DISTINCT COALESCE(st.title, 'untitled session')
                    FROM turns t
                    LEFT JOIN session_titles st ON st.session_id = t.session_id
                    WHERE t.started_at >= :since AND t.status = 'complete'
                    LIMIT 10
                    """
                ),
                {"since": since},
            )
            titles = [row[0] for row in r.fetchall()]
        return "; ".join(titles) if titles else "no sessions"
    except Exception:
        return "session data unavailable"


def _deterministic_brief(kind: str, sections: dict[str, str]) -> str:
    """A readable brief built WITHOUT the LLM, straight from the
    assembled sections. Every line is measured truth. Used when the
    API is down OR when the synthesised brief fails fact-validation —
    a plain true brief always beats a polished false one."""
    lines = [f"{kind.title()} brief — verified (synthesis withheld)"]
    for k, v in sections.items():
        if k == "compass":
            continue
        v = (v or "").strip()
        if v:
            lines.append(f"\n{k.upper()}:\n{v}")
    return "\n".join(lines)


def _nums_before(text: str, keywords: tuple[str, ...], gap: int = 26) -> list[int]:
    """Every integer that appears within `gap` chars BEFORE any of the
    keywords. Proximity, not adjacency — so "10 staged reply drafts"
    and "10 drafts" both bind 10 to 'draft'. The tight window keeps
    "top 3 today … drafts" from cross-binding."""
    out: list[int] = []
    low = text.lower()
    for kw in keywords:
        i = low.find(kw)
        while i != -1:
            window = low[max(0, i - gap):i]
            nums = re.findall(r"\d+", window)
            if nums:
                out.append(int(nums[-1]))  # nearest preceding number
            i = low.find(kw, i + len(kw))
    return out


def _validate(text: str, facts: dict, data_block: str) -> list[str]:
    """Reconcile the synthesised brief against the measured facts.
    Returns a list of drift reasons (empty == clean). Conservative —
    only flags an output number that CONTRADICTS a known metric, plus
    any unmonitored topic the model invented. Times, "top 3", list
    counts etc. are never flagged (no keyword nearby)."""
    drift: list[str] = []
    db_low = data_block.lower()

    # Email action-needed count.
    an = facts.get("action_needed")
    if an is not None:
        for n in _nums_before(
            text, ("action-needed", "action needed", "actionable", "unanswered")
        ):
            if n != an:
                drift.append(f"email action count {n}≠{an}")
                break

    # Draft count.
    dr = facts.get("drafts_ready")
    if dr is not None:
        for n in _nums_before(text, ("draft",)):
            if n != dr:
                drift.append(f"draft count {n}≠{dr}")
                break

    # Unmonitored topics the model has invented warnings about.
    low = text.lower()
    for term in _UNMONITORED:
        if term in low and term not in db_low:
            drift.append(f"unsourced topic '{term}'")

    return drift


async def _synthesize(
    kind: str, sections: dict[str, str], facts: dict | None = None
) -> str:
    """Claude turns assembled signal into the brief, then the output is
    RECONCILED against the measured facts. On drift: one hardened retry
    that restates the ground-truth numbers; still drifting → a
    deterministic brief built from the sections. Delivery never depends
    on the LLM being up, and a fabricated number never ships."""
    facts = facts or {}
    data_block = "\n".join(f"## {k}\n{v}" for k, v in sections.items() if v)
    fallback = _deterministic_brief(kind, sections)

    # SOURCE GUARD (P0b) — appended to whichever brief instruction runs.
    source_guard = (
        " SOURCE GUARD (critical): every number and named fact must come "
        "verbatim from the DATA below — never change a count, never round, "
        "never invent one. Raise a watch-out ONLY if it is grounded in the "
        "DATA; if nothing measured warrants one, write 'Watch-out: none "
        "flagged.' NEVER mention iCloud, Vercel, disk or storage quotas, or "
        "any system not present in the DATA — Astra does not measure those."
    )

    try:
        import anthropic

        from astra.config import settings

        if kind == "morning":
            instruction = (
                "Write Kunal's MORNING brief (≤250 words, plain text, no "
                "markdown headers — short labelled lines). Structure: "
                "TOP 3 today (specific, drawn from calendar/inbox/"
                "businesses/training, compass-weighted); then one tight "
                "line each for Inbox (mention staged drafts if any), "
                "Calendar, Businesses, Training; end with ONE watch-out. "
                "Decisions and actions, not summaries. Never invent data; "
                "if a section was unavailable, at most one clause says so."
            ) + source_guard
        else:
            instruction = (
                "Write Kunal's EVENING brief (≤250 words, plain text). "
                "Two sections: WHAT MOVED TODAY (from the sessions/"
                "inbox/business data — concrete, compass-weighted) and "
                "TOMORROW NEEDS (top 3, specific). End with one line on "
                "training. Honest about gaps; never invent."
            ) + source_guard

        client = anthropic.AsyncAnthropic()

        async def _attempt(instr: str) -> str:
            resp = await client.messages.create(
                model=settings.model_sonnet,
                max_tokens=700,
                system=(
                    "You are Astra, Kunal's agent OS, writing his daily brief. "
                    "Compass (what matters, in priority order):\n"
                    + sections.get("compass", "")
                ),
                messages=[
                    {"role": "user", "content": f"{instr}\n\nDATA:\n{data_block}"}
                ],
            )
            return "".join(
                b.text for b in resp.content if getattr(b, "type", "") == "text"
            ).strip()

        text = await _attempt(instruction)
        drift = _validate(text, facts, data_block)
        if not drift:
            return text or fallback

        # Drift caught — restate the exact numbers and try once more.
        logger.warning(
            "[briefing] %s synthesis drift %s — hardened retry", kind, drift
        )
        an = facts.get("action_needed")
        dr = facts.get("drafts_ready")
        ground = (
            "GROUND TRUTH you MUST restate exactly, changing no number: "
            f"inbox has {an if an is not None else 'an unknown number of'} "
            f"action-needed email(s) and "
            f"{dr if dr is not None else 'an unknown number of'} reply draft(s) "
            "staged. Do NOT mention iCloud, Vercel, disk, or storage. "
        )
        text2 = await _attempt(ground + instruction)
        drift2 = _validate(text2, facts, data_block)
        if drift2:
            logger.error(
                "[briefing] %s STILL drifting %s — deterministic fallback",
                kind,
                drift2,
            )
            return fallback
        return text2 or fallback
    except Exception as e:
        logger.warning("[briefing] synthesis failed (%s) — raw fallback", e)
        return fallback


async def _deliver(kind: str, body: str) -> dict[str, Any]:
    """memory (always) → push (attempt) → WhatsApp (best effort)."""
    from astra.db.engine import async_session
    from astra.memory.models import MemoryType
    from astra.memory.store import store_memory

    delivered: dict[str, Any] = {}

    async with async_session() as session:
        await store_memory(
            session=session,
            content=f"{kind.title()} briefing:\n{body}",
            memory_type=MemoryType.EPISODIC,
            source="scheduler",
            tags=f"briefing,{kind},daily,proactive",
            importance=0.5,
        )
    delivered["memory"] = True

    try:
        from astra.notifications import notify

        notify(
            title=f"astra · {kind} brief",
            body=body[:180],
            url="/briefing",
            tag=f"briefing-{kind}",
        )
        delivered["push"] = True
    except Exception:
        delivered["push"] = False

    # WhatsApp — the briefing's primary surface once the owner number
    # is configured.
    try:
        base = os.environ.get(
            "GATEWAY_URL", "http://whatsapp.railway.internal:8080"
        ).rstrip("/")
        headers = {
            "x-astra-secret": os.environ.get("AGENT_SHARED_SECRET", "").strip()
        }
        async with httpx.AsyncClient(timeout=30.0) as c:
            r = await c.post(
                f"{base}/api/v1/notify/owner",
                json={"text": body},
                headers=headers,
            )
            delivered["whatsapp"] = (
                r.status_code == 200 and (r.json() or {}).get("ok", False)
            )
            if not delivered["whatsapp"]:
                logger.info(
                    "[briefing] WA delivery skipped/failed: %s", r.text[:150]
                )
    except Exception as e:
        delivered["whatsapp"] = False
        logger.info("[briefing] WA delivery unavailable: %s", e)

    return delivered


async def morning_briefing_v2() -> dict:
    inbox_txt, inbox_facts = await _inbox_state()
    fleet_txt, fleet_facts = await _fleet_line()
    training_txt, training_facts = await _training_state()
    sections = {
        "compass": _compass_text(),
        "calendar today (IST)": await _calendar_today(),
        "inbox": inbox_txt,
        "businesses/fleet": fleet_txt,
        "training": training_txt,
        "research": await _research_line(),
    }
    facts = {**inbox_facts, **fleet_facts, **training_facts}
    body = await _synthesize("morning", sections, facts)
    delivered = await _deliver("morning", body)
    logger.info(
        "[scheduler] morning_briefing_v2 delivered: %s", delivered
    )
    return {"status": "success", "briefing": body, "delivered": delivered}


async def evening_briefing_v2() -> dict:
    inbox_txt, inbox_facts = await _inbox_state()
    fleet_txt, fleet_facts = await _fleet_line()
    training_txt, training_facts = await _training_state()
    sections = {
        "compass": _compass_text(),
        "sessions today": await _recent_turn_topics(),
        "inbox": inbox_txt,
        "businesses/fleet": fleet_txt,
        "training": training_txt,
        "calendar tomorrow": await _calendar_tomorrow(),
    }
    facts = {**inbox_facts, **fleet_facts, **training_facts}
    body = await _synthesize("evening", sections, facts)
    delivered = await _deliver("evening", body)
    logger.info(
        "[scheduler] evening_briefing_v2 delivered: %s", delivered
    )
    return {"status": "success", "briefing": body, "delivered": delivered}


async def _calendar_tomorrow() -> str:
    from astra.db.engine import async_session

    ist_now = _now_ist()
    day_start = ist_now.replace(
        hour=0, minute=0, second=0, microsecond=0
    ) + timedelta(days=1)
    start_utc = day_start - _IST_OFFSET
    end_utc = start_utc + timedelta(days=1)
    try:
        async with async_session() as s:
            r = await s.execute(
                _sql(
                    """
                    SELECT summary, start_at, is_all_day
                    FROM calendar_events
                    WHERE status != 'cancelled'
                      AND start_at >= :a AND start_at < :b
                    ORDER BY start_at LIMIT 8
                    """
                ),
                {"a": start_utc, "b": end_utc},
            )
            rows = r.fetchall()
        if not rows:
            return "nothing scheduled yet"
        out = []
        for summary, start_at, all_day in rows:
            t = "all-day" if all_day else (start_at + _IST_OFFSET).strftime("%H:%M")
            out.append(f"- {t} {summary}")
        return "\n".join(out)
    except Exception:
        return "calendar unavailable"
