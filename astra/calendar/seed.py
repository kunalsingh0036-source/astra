"""
Propose Kunal's weekly training + work scaffold to Google Calendar.

Source of truth: kunal_compass.md daily schedule block (2026-04-19).
14 recurring events covering weekday 05:30–22:00 IST:

  05:30–06:00  Stretch
  06:00–06:30  Yoga
  06:30–07:00  Breathing
  07:00–07:30  Meditation
  07:30–08:00  Commute to CCI
  08:00–09:00  Movement (CCI)
  09:00–09:30  Transit to Badhwar Park
  09:30–11:00  Squash (Badhwar Park)
  11:00–11:30  Transit to Ascend gym
  11:30–13:00  Gym (Ascend, Cuffe Parade)
  13:00–18:00  Work window (HelmTech / Apex / Bay / Top Studios)
  18:00–20:30  Evening training (squash + skill)
  20:30–21:30  Commute home + dinner
  21:30–22:00  Astra catch-up + briefing

All as RRULE=FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR (weekdays only).
The first occurrence anchors on the next upcoming Monday so
subsequent weeks repeat cleanly.

Each row is staged as a separate proposal so Kunal can approve /
reject individually if one block doesn't fit.
"""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)


IST = timezone(timedelta(hours=5, minutes=30))


# (summary, start HH:MM, end HH:MM, description, location)
SCAFFOLD: list[tuple[str, str, str, str, str]] = [
    ("Stretch", "05:30", "06:00", "10 min mobility + stretch. Never interrupt.", "Home · Charni Road"),
    ("Yoga", "06:00", "06:30", "Yoga. Never interrupt.", "Home · Charni Road"),
    ("Breathing", "06:30", "07:00", "Breathing practice. Never interrupt.", "Home · Charni Road"),
    ("Meditation", "07:00", "07:30", "Meditation. Never interrupt.", "Home · Charni Road"),
    ("Transit to CCI", "07:30", "08:00", "Commute from Charni Road to Cricket Club of India.", ""),
    ("Movement training", "08:00", "09:00", "Movement training at CCI. Never interrupt.", "CCI · Churchgate"),
    ("Transit to Badhwar Park", "09:00", "09:30", "CCI to Badhwar Park (BAY's court).", ""),
    ("Squash — main session", "09:30", "11:00", "Primary squash training. Never interrupt.", "Badhwar Park · Colaba"),
    ("Transit to Ascend", "11:00", "11:30", "Badhwar Park to Ascend sand gym, Cuffe Parade.", ""),
    ("Gym", "11:30", "13:00", "Strength + conditioning. Never interrupt.", "Ascend · Cuffe Parade"),
    (
        "Work window",
        "13:00",
        "18:00",
        (
            "Only window for meetings. Default meeting slot 13:30–17:30 IST. "
            "HelmTech, Apex, BAY, Top Studios."
        ),
        "",
    ),
    (
        "Evening training",
        "18:00",
        "20:30",
        "Squash + skill training. Never interrupt.",
        "CCI or Badhwar Park",
    ),
    ("Commute + dinner", "20:30", "21:30", "Commute home, dinner.", ""),
    (
        "Astra check-in",
        "21:30",
        "22:00",
        "21:30 catch-up prompt lands · 22:00 evening briefing. Log training via /tonight.",
        "",
    ),
]


def _next_monday(now_ist: datetime) -> datetime.date:
    """Return the next Monday's date in IST (or today if it's Monday)."""
    today = now_ist.astimezone(IST).date()
    # weekday(): Mon=0 … Sun=6
    delta = (0 - today.weekday()) % 7
    return today + timedelta(days=delta)


def _utc_from_ist_time(anchor_date, hhmm: str) -> datetime:
    h, m = [int(x) for x in hhmm.split(":")]
    ist_dt = datetime.combine(anchor_date, time(h, m), tzinfo=IST)
    return ist_dt.astimezone(timezone.utc)


WEEKLY_WEEKDAYS = "RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"


async def seed_weekly_scaffold(*, calendar_id: str = "primary") -> dict[str, Any]:
    """Stage 14 pending proposals — one per block in the weekday scaffold.

    Idempotent against same-day re-runs: if proposals with
    source='scaffold-seed' already exist in state pending / approved /
    applied, we skip creating duplicates.
    """
    from sqlalchemy import text

    from astra.calendar.writeback import propose_event
    from astra.db.engine import async_session

    async with async_session() as session:
        existing = await session.execute(
            text(
                """
                SELECT COUNT(*) FROM calendar_event_proposals
                WHERE source = 'scaffold-seed'
                  AND status IN ('pending', 'approved', 'applied')
                """
            )
        )
        count = int(existing.scalar_one())
    if count > 0:
        logger.info(
            "[seed] scaffold already has %s active proposals — skipping", count
        )
        return {
            "status": "skipped",
            "reason": f"{count} active scaffold proposals already staged",
        }

    anchor = _next_monday(datetime.now(IST))
    proposal_ids: list[int] = []

    for summary, s_hhmm, e_hhmm, desc, loc in SCAFFOLD:
        start_utc = _utc_from_ist_time(anchor, s_hhmm)
        end_utc = _utc_from_ist_time(anchor, e_hhmm)
        pid = await propose_event(
            summary=summary,
            start_at=start_utc,
            end_at=end_utc,
            description=desc,
            location=loc,
            tz="Asia/Kolkata",
            is_all_day=False,
            attendees=None,
            recurrence_rule=WEEKLY_WEEKDAYS,
            calendar_id=calendar_id,
            source="scaffold-seed",
            kind="create",
        )
        proposal_ids.append(pid)

    logger.info(
        "[seed] proposed %d scaffold events anchored %s",
        len(proposal_ids), anchor.isoformat(),
    )
    return {
        "status": "success",
        "anchor_monday": anchor.isoformat(),
        "proposed_count": len(proposal_ids),
        "proposal_ids": proposal_ids,
    }
