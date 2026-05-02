"""
Calendar event storage layer — upsert + query.

All date math uses tz-aware datetimes in UTC. Callers that render in
IST should convert at the rendering layer, not at storage.
"""

from __future__ import annotations

import json
from datetime import datetime, time, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import text

from astra.db.engine import async_session


IST = timezone(timedelta(hours=5, minutes=30))


async def upsert_event(
    *,
    google_id: str,
    calendar_id: str = "primary",
    summary: str,
    description: str,
    location: str,
    start_at: datetime | None,
    end_at: datetime | None,
    tz: str,
    is_all_day: bool,
    attendees_json: str,
    meet_link: str,
    status: str,
    organizer_email: str,
    creator_email: str,
    etag: str,
) -> str:
    """Insert or update a calendar_events row.

    Returns 'inserted' | 'updated' | 'unchanged' based on whether the
    etag changed (cheap change detection). On etag match we still
    bump last_synced_at so the row doesn't look stale.
    """
    async with async_session() as session:
        existing = await session.execute(
            text(
                "SELECT id, etag FROM calendar_events WHERE google_id = :gid"
            ),
            {"gid": google_id},
        )
        row = existing.first()

        params = {
            "gid": google_id,
            "cal": calendar_id,
            "sum": summary,
            "desc": description,
            "loc": location,
            "sta": start_at,
            "ena": end_at,
            "tz": tz,
            "all": is_all_day,
            "att": attendees_json,
            "meet": meet_link,
            "st": status,
            "org": organizer_email,
            "cre": creator_email,
            "etag": etag,
        }

        if row is None:
            await session.execute(
                text(
                    """
                    INSERT INTO calendar_events
                      (google_id, calendar_id, summary, description, location,
                       start_at, end_at, tz, is_all_day, attendees_json,
                       meet_link, status, organizer_email, creator_email, etag,
                       first_seen_at, last_synced_at)
                    VALUES
                      (:gid, :cal, :sum, :desc, :loc,
                       :sta, :ena, :tz, :all, :att,
                       :meet, :st, :org, :cre, :etag,
                       now(), now())
                    """
                ),
                params,
            )
            await session.commit()
            return "inserted"

        _row_id, old_etag = row
        if old_etag == etag:
            # Still bump last_synced_at so staleness doesn't flag unchanged.
            await session.execute(
                text(
                    "UPDATE calendar_events SET last_synced_at = now() WHERE id = :id"
                ),
                {"id": _row_id},
            )
            await session.commit()
            return "unchanged"

        await session.execute(
            text(
                """
                UPDATE calendar_events SET
                  calendar_id = :cal,
                  summary = :sum,
                  description = :desc,
                  location = :loc,
                  start_at = :sta,
                  end_at = :ena,
                  tz = :tz,
                  is_all_day = :all,
                  attendees_json = :att,
                  meet_link = :meet,
                  status = :st,
                  organizer_email = :org,
                  creator_email = :cre,
                  etag = :etag,
                  last_synced_at = now()
                WHERE id = :id
                """
            ),
            {**params, "id": _row_id},
        )
        await session.commit()
        return "updated"


async def mark_stale_cancelled(
    *,
    calendar_id: str,
    run_started_at: datetime,
    window_start: datetime,
    window_end: datetime,
) -> int:
    """Flip rows the sync didn't see to status='cancelled'.

    Scoped to the window we just queried — we don't touch rows outside
    [window_start, window_end] since they were never expected to appear.

    Implementation note: previous version used `WHERE google_id NOT IN
    :present` with a Python tuple, but SQLAlchemy + asyncpg doesn't
    auto-expand a `text(...)` IN-bound tuple, so NOT IN matched
    nothing and every event in the window got incorrectly flipped to
    cancelled. We use `last_synced_at < run_started_at` instead — any
    row updated by the current sync gets its last_synced_at bumped to
    now(), so anything older than the run's start time genuinely wasn't
    seen and is safe to cancel. This is also much faster (no large
    array binding).
    """
    async with async_session() as session:
        result = await session.execute(
            text(
                """
                UPDATE calendar_events
                SET status = 'cancelled',
                    last_synced_at = now()
                WHERE calendar_id = :cal
                  AND status != 'cancelled'
                  AND start_at >= :ws
                  AND start_at <= :we
                  AND last_synced_at < :run_start
                RETURNING id
                """
            ),
            {
                "cal": calendar_id,
                "ws": window_start,
                "we": window_end,
                "run_start": run_started_at,
            },
        )
        count = len(result.all())
        await session.commit()
        return count


# ── queries ────────────────────────────────────────────────────────


def _ist_day_bounds_utc(day_ist: datetime) -> tuple[datetime, datetime]:
    """Return UTC (start, end) bounds for the given IST calendar day."""
    ist_date = day_ist.astimezone(IST).date()
    start_ist = datetime.combine(ist_date, time.min, tzinfo=IST)
    end_ist = datetime.combine(ist_date, time.max, tzinfo=IST)
    return start_ist.astimezone(timezone.utc), end_ist.astimezone(timezone.utc)


def _row_to_dict(r: Any) -> dict[str, Any]:
    return {
        "id": r[0],
        "google_id": r[1],
        "calendar_id": r[2],
        "summary": r[3],
        "description": r[4] or "",
        "location": r[5] or "",
        "start_at": r[6].isoformat() if r[6] else None,
        "end_at": r[7].isoformat() if r[7] else None,
        "tz": r[8] or "",
        "is_all_day": bool(r[9]),
        "attendees": _parse_attendees(r[10]),
        "meet_link": r[11] or "",
        "status": r[12] or "",
        "organizer_email": r[13] or "",
    }


def _parse_attendees(raw: str | None) -> list[dict]:
    if not raw:
        return []
    try:
        return json.loads(raw)
    except Exception:
        return []


_COMMON_SELECT = """
    SELECT id, google_id, calendar_id, summary, description, location,
           start_at, end_at, tz, is_all_day, attendees_json, meet_link,
           status, organizer_email
    FROM calendar_events
"""


async def list_events_between(
    start_utc: datetime,
    end_utc: datetime,
    *,
    include_cancelled: bool = False,
    limit: int = 200,
) -> list[dict[str, Any]]:
    where = ["start_at >= :s", "start_at < :e"]
    if not include_cancelled:
        where.append("status != 'cancelled'")
    clause = " AND ".join(where)
    async with async_session() as session:
        r = await session.execute(
            text(
                f"{_COMMON_SELECT} WHERE {clause} ORDER BY start_at ASC LIMIT :lim"
            ),
            {"s": start_utc, "e": end_utc, "lim": limit},
        )
        return [_row_to_dict(row) for row in r.all()]


async def list_events_today() -> list[dict[str, Any]]:
    s, e = _ist_day_bounds_utc(datetime.now(IST))
    return await list_events_between(s, e)


async def list_events_tomorrow() -> list[dict[str, Any]]:
    tomorrow = datetime.now(IST) + timedelta(days=1)
    s, e = _ist_day_bounds_utc(tomorrow)
    return await list_events_between(s, e)


async def search_events(
    query: str,
    *,
    window_days: int = 30,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Case-insensitive substring search over summary + description."""
    now = datetime.now(timezone.utc)
    async with async_session() as session:
        r = await session.execute(
            text(
                f"""
                {_COMMON_SELECT}
                WHERE status != 'cancelled'
                  AND start_at >= :s AND start_at < :e
                  AND (summary ILIKE :q OR description ILIKE :q
                       OR location ILIKE :q)
                ORDER BY start_at ASC
                LIMIT :lim
                """
            ),
            {
                "s": now - timedelta(days=window_days),
                "e": now + timedelta(days=window_days),
                "q": f"%{query}%",
                "lim": limit,
            },
        )
        return [_row_to_dict(row) for row in r.all()]
