"""
Calendar harvester — pull a rolling 14-day window from Google Calendar.

Strategy:
  * Every sync, request events in the [now - 2h, now + 14d] window.
  * Upsert by google event id. Delete-detection: any row whose
    google_id stopped appearing in two consecutive syncs within the
    window is marked status='cancelled'.
  * Store native tz + all-day flag verbatim.

Cost is low — Kunal's calendar has maybe 20–40 events in a 2-week
window. A full sync is <1 second and the bandwidth is negligible.
We skip incremental sync tokens for now; the window is so small
that a fresh list is simpler and correct.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)


# How far back and forward to pull. Past buffer lets the briefing
# narrate "what just happened today" accurately even if the sync
# drifted a few minutes late.
WINDOW_PAST = timedelta(hours=2)
WINDOW_FUTURE = timedelta(days=14)


@dataclass
class SyncReport:
    total_seen: int = 0
    upserted: int = 0
    cancelled: int = 0
    unchanged: int = 0
    elapsed_ms: int = 0
    error: str | None = None
    calendar_ids: list[str] = field(default_factory=list)


def _parse_google_dt(d: dict[str, Any]) -> tuple[datetime | None, str, bool]:
    """Return (utc_datetime, tz, is_all_day) from a google start/end dict.

    Google uses `dateTime` for timed events (with timezone) or `date`
    for all-day events (YYYY-MM-DD, no tz).
    """
    if "dateTime" in d:
        val = d["dateTime"]  # ISO 8601 with offset
        tz = d.get("timeZone", "")
        try:
            dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
            return dt.astimezone(timezone.utc), tz, False
        except Exception:
            return None, tz, False
    if "date" in d:
        val = d["date"]  # YYYY-MM-DD
        tz = d.get("timeZone", "")
        try:
            dt = datetime.fromisoformat(f"{val}T00:00:00+00:00")
            return dt, tz, True
        except Exception:
            return None, tz, True
    return None, "", False


def _extract_meet_link(ev: dict[str, Any]) -> str:
    """Return the first usable video conference URL, or empty."""
    # Preferred: the official conferenceData entryPoints.
    cd = ev.get("conferenceData") or {}
    for ep in cd.get("entryPoints", []) or []:
        uri = ep.get("uri", "")
        if uri.startswith("http"):
            return uri
    # Fallback: hangoutLink on the event itself.
    return ev.get("hangoutLink") or ""


async def sync_calendar(calendar_id: str = "primary") -> SyncReport:
    """Pull and upsert events for the rolling 14-day window.

    Async wrapper around the sync googleapiclient call — runs the
    blocking portion in a thread so we don't block the scheduler
    event loop.
    """
    from astra.calendar.client import get_calendar_service
    from astra.calendar.store import (
        mark_stale_cancelled,
        upsert_event,
    )

    report = SyncReport(calendar_ids=[calendar_id])
    t0 = datetime.now(timezone.utc)

    service = await asyncio.to_thread(get_calendar_service)
    if service is None:
        report.error = "calendar service unavailable (auth not set up?)"
        return report

    now = datetime.now(timezone.utc)
    time_min = (now - WINDOW_PAST).isoformat()
    time_max = (now + WINDOW_FUTURE).isoformat()

    page_token: str | None = None
    seen_google_ids: set[str] = set()

    while True:
        def _list():
            return (
                service.events()
                .list(
                    calendarId=calendar_id,
                    timeMin=time_min,
                    timeMax=time_max,
                    singleEvents=True,
                    orderBy="startTime",
                    maxResults=250,
                    pageToken=page_token,
                )
                .execute()
            )

        try:
            result = await asyncio.to_thread(_list)
        except Exception as e:
            logger.exception("[calendar] list failed: %s", e)
            report.error = str(e)[:400]
            break

        for ev in result.get("items", []):
            gid = ev.get("id")
            if not gid:
                continue
            seen_google_ids.add(gid)
            report.total_seen += 1

            start, stz, is_all = _parse_google_dt(ev.get("start", {}))
            end, _etz, _ea = _parse_google_dt(ev.get("end", {}))
            attendees = ev.get("attendees", []) or []
            trimmed = [
                {
                    "email": a.get("email", ""),
                    "name": a.get("displayName", ""),
                    "response": a.get("responseStatus", ""),
                    "organizer": bool(a.get("organizer", False)),
                }
                for a in attendees
            ]

            status = ev.get("status", "confirmed")
            summary = ev.get("summary", "") or ""
            description = ev.get("description", "") or ""
            location = ev.get("location", "") or ""
            organizer = (ev.get("organizer") or {}).get("email", "") or ""
            creator = (ev.get("creator") or {}).get("email", "") or ""
            meet = _extract_meet_link(ev)
            etag = ev.get("etag", "") or ""

            try:
                action = await upsert_event(
                    google_id=gid,
                    calendar_id=calendar_id,
                    summary=summary[:511],
                    description=description,
                    location=location[:511],
                    start_at=start,
                    end_at=end,
                    tz=stz[:63],
                    is_all_day=is_all,
                    attendees_json=json.dumps(trimmed),
                    meet_link=meet[:511],
                    status=status[:15],
                    organizer_email=organizer[:255],
                    creator_email=creator[:255],
                    etag=etag[:255],
                )
                if action == "inserted" or action == "updated":
                    report.upserted += 1
                else:
                    report.unchanged += 1
            except Exception as e:
                logger.warning(
                    "[calendar] upsert failed for %s: %s", gid, e
                )

        page_token = result.get("nextPageToken")
        if not page_token:
            break

    # Mark rows stale — events in the window whose last_synced_at is
    # older than this run started (i.e. weren't touched by upsert_event
    # during this sweep). Uses timestamp comparison instead of an
    # IN-tuple bind because asyncpg can't expand large tuples reliably.
    try:
        cancelled_count = await mark_stale_cancelled(
            calendar_id=calendar_id,
            run_started_at=t0,
            window_start=now - WINDOW_PAST,
            window_end=now + WINDOW_FUTURE,
        )
        report.cancelled = cancelled_count
    except Exception as e:
        logger.warning("[calendar] cancel-sweep failed: %s", e)

    report.elapsed_ms = int(
        (datetime.now(timezone.utc) - t0).total_seconds() * 1000
    )
    return report


async def sync_all() -> dict[str, Any]:
    """Sync the user's primary calendar. Shared-cal discovery is a
    later enhancement; for now the primary calendar covers Kunal's
    personal + work schedule via overlays.
    """
    report = await sync_calendar("primary")
    return {
        "status": "success" if report.error is None else "error",
        "error": report.error,
        "total_seen": report.total_seen,
        "upserted": report.upserted,
        "cancelled": report.cancelled,
        "unchanged": report.unchanged,
        "elapsed_ms": report.elapsed_ms,
    }
