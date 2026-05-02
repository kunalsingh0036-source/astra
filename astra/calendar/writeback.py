"""
Approval-gated calendar write pipeline.

Same pattern as astra/notes/writeback.py:
  1. `propose_event(...)` — stages a row in `calendar_event_proposals`
     with status='pending'. Never touches Google directly.
  2. Web / API flips status='approved' once Kunal clicks Apply.
  3. `apply_approved_proposals()` — 60-s scheduler worker that picks
     up approved rows and performs the Calendar API call.

Safety posture:
  * `calendar_write_mode` setting mirrors notes_writeback_mode:
    "approval" (default) / "auto" / "off".
  * A create that fails writes status='error' with the Google error
    message; the proposal is retained so Kunal can see what happened.
  * Idempotency: a successful create stores the new google_id in
    `resulting_google_id`. Subsequent re-runs of the worker on the
    same row are no-ops.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from astra.db.engine import async_session

logger = logging.getLogger(__name__)


# ─── propose ────────────────────────────────────────────────────────


async def propose_event(
    *,
    summary: str,
    start_at: datetime | None,
    end_at: datetime | None,
    description: str = "",
    location: str = "",
    tz: str = "Asia/Kolkata",
    is_all_day: bool = False,
    attendees: list[str] | None = None,
    recurrence_rule: str | None = None,
    calendar_id: str = "primary",
    source: str = "manual",
    kind: str = "create",
    google_id: str | None = None,
) -> int:
    """Stage a pending create/update/delete proposal.

    Returns the new row's id. Caller can redirect Kunal to
    `/calendar/propose` to review.

    For the common case (create), pass summary + start_at + end_at.
    For a recurring event, also pass `recurrence_rule` — a raw Google
    RRULE string like "RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR".
    """
    attendees_list = [a for a in (attendees or []) if a]
    async with async_session() as session:
        row = await session.execute(
            text(
                """
                INSERT INTO calendar_event_proposals
                  (kind, source, calendar_id, summary, description, location,
                   start_at, end_at, tz, is_all_day,
                   attendees_json, recurrence_json, google_id, status)
                VALUES
                  (:k, :src, :cal, :sum, :desc, :loc,
                   :sta, :ena, :tz, :ad,
                   :att, :rec, :gid, 'pending')
                RETURNING id
                """
            ),
            {
                "k": kind,
                "src": source,
                "cal": calendar_id,
                "sum": summary,
                "desc": description,
                "loc": location,
                "sta": start_at,
                "ena": end_at,
                "tz": tz,
                "ad": is_all_day,
                "att": json.dumps(attendees_list),
                "rec": recurrence_rule,
                "gid": google_id,
            },
        )
        proposal_id = int(row.scalar_one())
        await session.commit()
    logger.info(
        "[calendar-writeback] proposed id=%s kind=%s summary=%r",
        proposal_id, kind, summary,
    )
    return proposal_id


# ─── apply worker ───────────────────────────────────────────────────


def _build_gcal_body(row: dict[str, Any]) -> dict[str, Any]:
    """Translate a proposal row to a Google Calendar `events.insert`
    request body.

    Handles timed vs all-day, optional attendees, optional recurrence.
    """
    tz = row.get("tz") or "Asia/Kolkata"
    body: dict[str, Any] = {
        "summary": row.get("summary", ""),
        "description": row.get("description", "") or "",
        "location": row.get("location", "") or "",
    }

    if row.get("is_all_day"):
        # Google expects "date" YYYY-MM-DD for all-day events.
        sd = row["start_at"].astimezone(timezone.utc).date().isoformat()
        ed = row["end_at"].astimezone(timezone.utc).date().isoformat()
        body["start"] = {"date": sd}
        body["end"] = {"date": ed}
    else:
        body["start"] = {
            "dateTime": row["start_at"].isoformat(),
            "timeZone": tz,
        }
        body["end"] = {
            "dateTime": row["end_at"].isoformat(),
            "timeZone": tz,
        }

    attendees = _parse_attendees(row.get("attendees_json"))
    if attendees:
        body["attendees"] = [{"email": e} for e in attendees]

    rec = row.get("recurrence_json")
    if rec:
        # We store a single RRULE string. Google wants a list.
        body["recurrence"] = [rec]

    return body


def _parse_attendees(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except Exception:
        return []
    out: list[str] = []
    for item in data or []:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict) and item.get("email"):
            out.append(item["email"])
    return out


async def _apply_one(row: dict[str, Any]) -> tuple[str, str | None]:
    """Perform the Google Calendar API call for one proposal row.

    Returns (status, resulting_google_id_or_error).
      status='applied' → second element is the created google_id (on
        create) or the same google_id (on update/delete).
      status='error'   → second element is the error string.
    """
    from astra.calendar.client import get_calendar_service

    service = await asyncio.to_thread(get_calendar_service)
    if service is None:
        return "error", "calendar service unavailable (auth not set up)"

    kind = (row.get("kind") or "create").lower()
    cal = row.get("calendar_id") or "primary"

    try:
        if kind == "create":
            body = _build_gcal_body(row)

            def _insert():
                return service.events().insert(
                    calendarId=cal, body=body
                ).execute()

            ev = await asyncio.to_thread(_insert)
            return "applied", ev.get("id")

        if kind == "update":
            gid = row.get("google_id")
            if not gid:
                return "error", "update proposal missing google_id"
            body = _build_gcal_body(row)

            def _patch():
                return service.events().patch(
                    calendarId=cal, eventId=gid, body=body
                ).execute()

            ev = await asyncio.to_thread(_patch)
            return "applied", ev.get("id") or gid

        if kind == "delete":
            gid = row.get("google_id")
            if not gid:
                return "error", "delete proposal missing google_id"

            def _delete():
                return service.events().delete(
                    calendarId=cal, eventId=gid
                ).execute()

            await asyncio.to_thread(_delete)
            return "applied", gid

        return "error", f"unknown kind: {kind}"
    except Exception as e:
        return "error", str(e)[:600]


async def apply_approved_proposals() -> dict[str, Any]:
    """Scheduler worker — picks up approved proposals and applies them.

    Also expires pending rows older than 48h (longer than notes' 24h
    window because a scaffold seed might sit unapproved for a day).
    """
    applied_ids: list[int] = []
    errored: list[dict[str, Any]] = []
    expired = 0

    async with async_session() as session:
        exp = await session.execute(
            text(
                """
                UPDATE calendar_event_proposals
                SET status = 'expired'
                WHERE status = 'pending'
                  AND created_at < now() - interval '48 hours'
                RETURNING id
                """
            )
        )
        expired = len(exp.all())

        rows = await session.execute(
            text(
                """
                SELECT id, kind, source, calendar_id, summary, description,
                       location, start_at, end_at, tz, is_all_day,
                       attendees_json, recurrence_json, google_id
                FROM calendar_event_proposals
                WHERE status = 'approved'
                ORDER BY approved_at ASC
                LIMIT 20
                """
            )
        )
        pending = [
            {
                "id": r[0],
                "kind": r[1],
                "source": r[2],
                "calendar_id": r[3],
                "summary": r[4],
                "description": r[5],
                "location": r[6],
                "start_at": r[7],
                "end_at": r[8],
                "tz": r[9],
                "is_all_day": r[10],
                "attendees_json": r[11],
                "recurrence_json": r[12],
                "google_id": r[13],
            }
            for r in rows.all()
        ]

        for row in pending:
            status, payload = await _apply_one(row)
            if status == "applied":
                await session.execute(
                    text(
                        """
                        UPDATE calendar_event_proposals
                        SET status = 'applied',
                            applied_at = now(),
                            resulting_google_id = :gid,
                            error = NULL
                        WHERE id = :id
                        """
                    ),
                    {"id": row["id"], "gid": payload},
                )
                applied_ids.append(row["id"])
            else:
                await session.execute(
                    text(
                        """
                        UPDATE calendar_event_proposals
                        SET status = 'error',
                            error = :err
                        WHERE id = :id
                        """
                    ),
                    {"id": row["id"], "err": payload},
                )
                errored.append({"id": row["id"], "error": payload})

        await session.commit()

    logger.info(
        "[calendar-writeback] applied=%s errored=%s expired=%s",
        applied_ids, errored, expired,
    )
    return {
        "applied_ids": applied_ids,
        "errored": errored,
        "expired_count": expired,
    }
