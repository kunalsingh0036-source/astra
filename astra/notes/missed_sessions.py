"""
Missed-session snapshot + trendline.

The "Kunal" Apple Note carries a running debt counter of missed
training sessions per type (stretch, meditate, breathe, movement,
skill, workout). Saturday and Sunday are designated catch-up days —
the counters should fall across the weekend and ideally hold flat
during the week.

This module:
  1. Parses the current counters out of the note body_text each time
     it's called. Format is tolerant — it handles line variations
     like "Stretch - 311", "Breathe- 205", "Skill - 178".
  2. Snapshots the 6 counters daily into `missed_session_snapshots`.
  3. Produces a week-over-week delta + 7-day series so the evening
     briefing can say exactly which types closed gap and which
     grew.

Interpretation rule (from kunal_compass.md, 2026-04-19):
  A decreasing counter = recovery on schedule.
  An increasing counter = compass slipping.

No inference beyond that. If the note's shape changes, the parser
returns partial data rather than guessing.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, text

logger = logging.getLogger(__name__)


# The six training types we track, in canonical order.
TYPES: tuple[str, ...] = (
    "stretch",
    "meditate",
    "breathe",
    "movement",
    "skill",
    "workout",
)

# Tolerant pattern — accepts "Stretch - 311", "Meditate-317",
# "Skill -178", "Workout-178". Case-insensitive.
_COUNTER_RE = re.compile(
    r"\b(stretch|meditate|breathe|breathing|movement|skill|workout)\s*[-–—:]\s*(\d+)",
    re.IGNORECASE,
)

# "breathing" appears as an interchangeable form for "breathe" in
# Kunal's note header. Normalize.
_ALIASES: dict[str, str] = {
    "breathing": "breathe",
}


@dataclass
class Counters:
    """One row of missed-session counters parsed from the note."""

    stretch: int | None = None
    meditate: int | None = None
    breathe: int | None = None
    movement: int | None = None
    skill: int | None = None
    workout: int | None = None

    def as_dict(self) -> dict[str, int | None]:
        return {k: getattr(self, k) for k in TYPES}

    def missing_types(self) -> list[str]:
        return [t for t in TYPES if getattr(self, t) is None]


def parse_counters(note_body_text: str) -> Counters:
    """Return a Counters with whatever numbers we could find.

    Parses the FIRST occurrence of each counter after the
    "Saturday- Sunday" (or similar) section header, since that's
    where Kunal tracks missed counts. If that anchor isn't found,
    falls back to scanning the whole note — some entries may reflect
    the daily-target header instead (e.g. "1 hr- Workout"), but those
    don't match our numeric pattern since they use "hr" as the unit.
    """
    counts: dict[str, int] = {}

    # Try to anchor at the Saturday–Sunday section — the counters
    # live below it.
    anchor = re.search(r"saturday\s*[-–—]\s*sunday", note_body_text, re.IGNORECASE)
    section = note_body_text[anchor.end():] if anchor else note_body_text

    for m in _COUNTER_RE.finditer(section):
        raw_type = m.group(1).lower()
        canon = _ALIASES.get(raw_type, raw_type)
        # Only keep the first hit per type so we don't clobber with
        # a later stray number (e.g. a different table below).
        if canon not in counts:
            counts[canon] = int(m.group(2))

    return Counters(
        stretch=counts.get("stretch"),
        meditate=counts.get("meditate"),
        breathe=counts.get("breathe"),
        movement=counts.get("movement"),
        skill=counts.get("skill"),
        workout=counts.get("workout"),
    )


async def current_counters() -> Counters | None:
    """Current counters. The cloud `training_counters` row is the source of
    truth (updatable over WhatsApp); the "Kunal" Apple Note is a bootstrap/
    fallback for before that row is seeded.

    Cloud-first is what keeps training context LIVE when the Mac/bridge is
    down (the macOS fault-line). See astra/notes/training_state.py.
    """
    try:
        from astra.notes.training_state import get_cloud_counters

        cloud = await get_cloud_counters()
        if cloud is not None:
            return cloud
    except Exception:
        pass  # fall back to the note below

    from astra.notes.store import search_notes

    rows = await search_notes("Kunal", limit=1)
    if not rows:
        return None
    body = rows[0].get("body_text", "") or ""
    return parse_counters(body)


async def snapshot_today(*, force: bool = False) -> dict[str, Any]:
    """Write (or refresh) today's row in `missed_session_snapshots`.

    Idempotent per UTC calendar day — we update the existing row if
    it's already been written today, so repeated calls converge on
    the latest parse. Pass `force=True` to unconditionally rewrite.
    """
    from astra.db.engine import async_session

    counters = await current_counters()
    if counters is None:
        logger.warning("[missed] snapshot_today: no 'Kunal' note found")
        return {"status": "skipped", "reason": "no Kunal note"}

    today_utc = datetime.now(timezone.utc).date()

    async with async_session() as session:
        # Check existing
        existing = await session.execute(
            text(
                """
                SELECT id FROM missed_session_snapshots
                WHERE snapshot_date = :d
                """
            ),
            {"d": today_utc},
        )
        row_id = existing.scalar_one_or_none()

        if row_id is None:
            await session.execute(
                text(
                    """
                    INSERT INTO missed_session_snapshots
                      (snapshot_date, stretch, meditate, breathe, movement, skill, workout,
                       raw_missing)
                    VALUES (:d, :s, :m, :b, :mv, :sk, :w, :missing)
                    """
                ),
                {
                    "d": today_utc,
                    "s": counters.stretch,
                    "m": counters.meditate,
                    "b": counters.breathe,
                    "mv": counters.movement,
                    "sk": counters.skill,
                    "w": counters.workout,
                    "missing": ",".join(counters.missing_types()),
                },
            )
            action = "inserted"
        else:
            await session.execute(
                text(
                    """
                    UPDATE missed_session_snapshots
                    SET stretch = :s, meditate = :m, breathe = :b,
                        movement = :mv, skill = :sk, workout = :w,
                        raw_missing = :missing
                    WHERE id = :id
                    """
                ),
                {
                    "id": row_id,
                    "s": counters.stretch,
                    "m": counters.meditate,
                    "b": counters.breathe,
                    "mv": counters.movement,
                    "sk": counters.skill,
                    "w": counters.workout,
                    "missing": ",".join(counters.missing_types()),
                },
            )
            action = "updated"

        await session.commit()

    logger.info(
        "[missed] snapshot_today %s: %s (missing=%s)",
        action,
        counters.as_dict(),
        counters.missing_types(),
    )
    return {
        "status": "success",
        "action": action,
        "snapshot_date": today_utc.isoformat(),
        "counters": counters.as_dict(),
        "missing": counters.missing_types(),
    }


async def trend(days: int = 14) -> dict[str, Any]:
    """Return the recent trajectory of the six counters.

    Shape:
      {
        "today":    {stretch: 311, ...} or None,
        "yesterday":{...}                or None,
        "week_ago": {...}                or None,
        "wow_delta":{stretch: +3, ...}   or None    # this_week - last_week
        "direction":{stretch: "gap closed", meditate: "flat", ...}
        "series":   [ {date, counters...}, ... ]    # oldest → newest
      }

    Semantics:
      decreasing counter → "gap closed" (good)
      increasing counter → "gap grew" (bad)
      unchanged          → "flat"
    """
    from astra.db.engine import async_session

    async with async_session() as session:
        # Use make_interval to sidestep the ':' vs '::' parser collision
        # SQLAlchemy has with Postgres's `::type` cast syntax.
        rows = await session.execute(
            text(
                """
                SELECT snapshot_date, stretch, meditate, breathe,
                       movement, skill, workout
                FROM missed_session_snapshots
                WHERE snapshot_date >= CURRENT_DATE - make_interval(days => :d)
                ORDER BY snapshot_date ASC
                """
            ),
            {"d": days},
        )
        series = [
            {
                "date": r[0].isoformat() if r[0] else None,
                "stretch": r[1],
                "meditate": r[2],
                "breathe": r[3],
                "movement": r[4],
                "skill": r[5],
                "workout": r[6],
            }
            for r in rows.all()
        ]

    def _find_on(offset_days: int) -> dict | None:
        target = (datetime.now(timezone.utc).date() - timedelta(days=offset_days)).isoformat()
        # Walk newest→oldest for closest-on-or-before.
        for row in reversed(series):
            if row["date"] and row["date"] <= target:
                return row
        return None

    today = _find_on(0)
    yesterday = _find_on(1)
    week_ago = _find_on(7)

    wow_delta: dict[str, int | None] | None = None
    direction: dict[str, str] | None = None
    if today is not None and week_ago is not None:
        wow_delta = {}
        direction = {}
        for t in TYPES:
            t_now = today.get(t)
            t_prev = week_ago.get(t)
            if t_now is None or t_prev is None:
                wow_delta[t] = None
                direction[t] = "unknown"
                continue
            delta = t_now - t_prev
            wow_delta[t] = delta
            if delta < 0:
                direction[t] = "gap closed"
            elif delta > 0:
                direction[t] = "gap grew"
            else:
                direction[t] = "flat"

    return {
        "today": today,
        "yesterday": yesterday,
        "week_ago": week_ago,
        "wow_delta": wow_delta,
        "direction": direction,
        "series": series,
    }
