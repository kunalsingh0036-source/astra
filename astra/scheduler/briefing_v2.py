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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import text as _sql

logger = logging.getLogger(__name__)

_IST_OFFSET = timedelta(hours=5, minutes=30)


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


async def _inbox_state() -> str:
    """Counts + action-needed heads + staged draft count via the
    email agent (mesh HTTP)."""
    try:
        from astra.email.client import get_summary, list_messages, mesh_headers, BASE_URL

        summary = await get_summary()
        if not summary:
            return "email agent unreachable"
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

        drafts_line = ""
        try:
            async with httpx.AsyncClient(timeout=8.0) as c:
                r = await c.get(
                    f"{BASE_URL}/api/v1/drafts/", headers=mesh_headers()
                )
                if r.status_code == 200:
                    ready = [
                        d for d in (r.json() or []) if d.get("status") == "ready"
                    ]
                    if ready:
                        drafts_line = (
                            f"\n{len(ready)} reply draft(s) staged and waiting "
                            "for review on /email"
                        )
        except Exception:
            pass

        lines = [f"{total} synced · {unread} unread · {action} action-needed"]
        lines += heads
        return "\n".join(lines) + drafts_line
    except Exception as e:
        logger.warning("[briefing] inbox read failed: %s", e)
        return "email agent unavailable"


async def _fleet_line() -> str:
    try:
        from astra.scheduler.jobs import _cloud_fleet_probe

        results = await _cloud_fleet_probe()
        bad = [r["service"] for r in results if r["status"] != "healthy"]
        if bad:
            return f"{len(results)-len(bad)}/{len(results)} healthy — DOWN: {', '.join(bad)}"
        return f"all {len(results)} services healthy"
    except Exception:
        return "fleet probe unavailable"


async def _training_state() -> str:
    """Latest missed-session snapshot → debt line. Snapshot freshness
    is laptop-dependent (macOS job), so age is reported honestly."""
    from astra.db.engine import async_session

    try:
        async with async_session() as s:
            r = await s.execute(
                _sql(
                    """
                    SELECT snapshot_date, stretch, meditate, breathe,
                           movement, skill, workout
                    FROM missed_session_snapshots
                    ORDER BY snapshot_date DESC LIMIT 1
                    """
                )
            )
            row = r.first()
        if not row:
            return "no training snapshot yet"
        date = row[0]
        debts = dict(
            zip(
                ("stretch", "meditate", "breathe", "movement", "skill", "workout"),
                row[1:],
            )
        )
        owed = {k: v for k, v in debts.items() if (v or 0) > 0}
        age_days = (_now_ist().date() - date).days
        stale = f" (snapshot {age_days}d old)" if age_days > 1 else ""
        if not owed:
            return f"no missed sessions{stale}"
        debt_str = ", ".join(f"{k}×{v}" for k, v in owed.items())
        return f"owed: {debt_str}{stale}"
    except Exception as e:
        logger.warning("[briefing] training read failed: %s", e)
        return "training data unavailable"


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


async def _synthesize(kind: str, sections: dict[str, str]) -> str:
    """Claude turns assembled signal into the brief. Falls back to a
    plain assembled digest if the API call fails — delivery must
    never depend on the LLM being up."""
    data_block = "\n".join(
        f"## {k}\n{v}" for k, v in sections.items() if v
    )
    fallback = f"{kind.title()} briefing (raw)\n\n{data_block}"

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
            )
        else:
            instruction = (
                "Write Kunal's EVENING brief (≤250 words, plain text). "
                "Two sections: WHAT MOVED TODAY (from the sessions/"
                "inbox/business data — concrete, compass-weighted) and "
                "TOMORROW NEEDS (top 3, specific). End with one line on "
                "training. Honest about gaps; never invent."
            )

        client = anthropic.AsyncAnthropic()
        resp = await client.messages.create(
            model=settings.model_sonnet,
            max_tokens=700,
            system=(
                "You are Astra, Kunal's agent OS, writing his daily brief. "
                "Compass (what matters, in priority order):\n"
                + sections.get("compass", "")
            ),
            messages=[
                {
                    "role": "user",
                    "content": f"{instruction}\n\nDATA:\n{data_block}",
                }
            ],
        )
        text = "".join(
            b.text for b in resp.content if getattr(b, "type", "") == "text"
        ).strip()
        return text or fallback
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
    sections = {
        "compass": _compass_text(),
        "calendar today (IST)": await _calendar_today(),
        "inbox": await _inbox_state(),
        "businesses/fleet": await _fleet_line(),
        "training": await _training_state(),
        "research": await _research_line(),
    }
    body = await _synthesize("morning", sections)
    delivered = await _deliver("morning", body)
    logger.info(
        "[scheduler] morning_briefing_v2 delivered: %s", delivered
    )
    return {"status": "success", "briefing": body, "delivered": delivered}


async def evening_briefing_v2() -> dict:
    sections = {
        "compass": _compass_text(),
        "sessions today": await _recent_turn_topics(),
        "inbox": await _inbox_state(),
        "businesses/fleet": await _fleet_line(),
        "training": await _training_state(),
        "calendar tomorrow": await _calendar_tomorrow(),
    }
    body = await _synthesize("evening", sections)
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
