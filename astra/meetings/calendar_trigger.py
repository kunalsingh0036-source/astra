"""
Calendar-triggered auto-capture.

Runs every minute. Three responsibilities:

  1. `schedule_upcoming()` — find calendar events starting in the next
     5 minutes that have a `meet_link` (Google Meet/Zoom/etc.), insert
     a `capture_sessions` row with state='scheduled'. Idempotent by
     (google_id, planned_start_at).

  2. `start_due()` — rows in state='scheduled' whose planned_start_at
     has arrived get an astra-capture subprocess launched; state
     flips to 'recording'. We record for `(end - start) + 5min` to
     catch meetings that run long.

  3. `stop_overdue()` — rows in state='recording' whose max duration
     has elapsed get SIGTERMed. The Swift binary finalizes the m4a
     and exits. Phase 1 pipeline then picks up the file on its next
     30-s tick.

Only fires on events that have a `meet_link`. In-person meetings are
left alone. Kunal can always kick off an ad-hoc capture manually via
the same Swift binary.

Buffer minutes:
  * `PRE_BUFFER_SECONDS`: start this many seconds BEFORE the event's
    scheduled start, so handshake/intro isn't missed.
  * `POST_BUFFER_SECONDS`: continue this many seconds past the event's
    end_at, for running-long meetings.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from astra.db.engine import async_session

logger = logging.getLogger(__name__)


# How many minutes ahead we look for upcoming events each tick.
LOOKAHEAD_MINUTES = 5
PRE_BUFFER_SECONDS = 30       # start 30s before event
POST_BUFFER_SECONDS = 300     # continue 5min past end


async def schedule_upcoming() -> int:
    """Insert a scheduled row for each upcoming event with a meet_link
    in the next LOOKAHEAD_MINUTES."""
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(minutes=LOOKAHEAD_MINUTES)

    inserted = 0
    async with async_session() as session:
        rows = await session.execute(
            text(
                """
                SELECT google_id, summary, start_at, end_at, meet_link
                FROM calendar_events
                WHERE status != 'cancelled'
                  AND meet_link <> ''
                  AND start_at >= :now
                  AND start_at <= :horizon
                """
            ),
            {"now": now, "horizon": horizon},
        )
        for r in rows.all():
            gid, summary, start_at, end_at, _meet = r
            if start_at is None or end_at is None:
                continue
            from astra.meetings.capture import output_path_for

            out = output_path_for(gid, summary or "meeting", start_at)

            # Idempotent: unique constraint on (google_id, planned_start_at)
            try:
                await session.execute(
                    text(
                        """
                        INSERT INTO capture_sessions
                          (calendar_event_google_id, summary,
                           planned_start_at, planned_end_at,
                           output_path, state)
                        VALUES
                          (:gid, :sum, :start, :end, :path, 'scheduled')
                        ON CONFLICT (calendar_event_google_id, planned_start_at)
                        DO NOTHING
                        """
                    ),
                    {
                        "gid": gid,
                        "sum": (summary or "")[:511],
                        "start": start_at,
                        "end": end_at,
                        "path": str(out),
                    },
                )
                inserted += 1
            except Exception as e:
                logger.warning("[capture-trigger] insert failed for %s: %s", gid, e)

        await session.commit()
    return inserted


async def start_due() -> list[int]:
    """Start recordings for scheduled rows whose start time has come."""
    from astra.meetings.capture import start_capture, is_available
    from pathlib import Path

    now = datetime.now(timezone.utc)
    started_ids: list[int] = []

    async with async_session() as session:
        rows = await session.execute(
            text(
                """
                SELECT id, summary, planned_start_at, planned_end_at, output_path
                FROM capture_sessions
                WHERE state = 'scheduled'
                  AND planned_start_at <= :buffer_point
                ORDER BY planned_start_at ASC
                LIMIT 5
                """
            ),
            {"buffer_point": now + timedelta(seconds=PRE_BUFFER_SECONDS)},
        )
        due = [
            (r[0], r[1], r[2], r[3], r[4]) for r in rows.all()
        ]

        for cid, summary, p_start, p_end, path in due:
            if not is_available():
                await session.execute(
                    text(
                        """
                        UPDATE capture_sessions
                        SET state='failed', error=:e, updated_at=now()
                        WHERE id=:id
                        """
                    ),
                    {"id": cid, "e": "capture binary unavailable"},
                )
                continue

            max_s = int(
                (p_end - p_start).total_seconds() + POST_BUFFER_SECONDS
            )
            # Hard cap — 4h. Prevents runaway from a bad end_at.
            max_s = min(max_s, 14_400)

            res = start_capture(Path(path), max_seconds=max_s)
            if res.get("status") == "started":
                await session.execute(
                    text(
                        """
                        UPDATE capture_sessions
                        SET state='recording',
                            pid=:pid,
                            started_at=now(),
                            updated_at=now(),
                            error=NULL
                        WHERE id=:id
                        """
                    ),
                    {"id": cid, "pid": res["pid"]},
                )
                started_ids.append(cid)
                logger.info(
                    "[capture-trigger] started cid=%s pid=%s summary=%r max=%ss",
                    cid, res["pid"], summary, max_s,
                )
            else:
                await session.execute(
                    text(
                        """
                        UPDATE capture_sessions
                        SET state='failed',
                            error=:e,
                            updated_at=now()
                        WHERE id=:id
                        """
                    ),
                    {"id": cid, "e": str(res.get("error", "unknown"))[:900]},
                )
                logger.warning(
                    "[capture-trigger] start failed cid=%s: %s",
                    cid, res.get("error"),
                )

        await session.commit()
    return started_ids


async def stop_overdue() -> list[int]:
    """SIGTERM any recording session whose planned end + post-buffer has passed,
    or whose pid has already exited."""
    from astra.meetings.capture import is_pid_alive, stop_capture

    now = datetime.now(timezone.utc)
    stopped_ids: list[int] = []

    async with async_session() as session:
        rows = await session.execute(
            text(
                """
                SELECT id, pid, planned_end_at
                FROM capture_sessions
                WHERE state = 'recording'
                """
            )
        )
        for r in rows.all():
            cid, pid, p_end = r[0], r[1], r[2]
            deadline = p_end + timedelta(seconds=POST_BUFFER_SECONDS)
            past_deadline = now >= deadline

            if pid is not None and not is_pid_alive(pid):
                # Process already exited on its own (max-seconds timer).
                await session.execute(
                    text(
                        """
                        UPDATE capture_sessions
                        SET state='finished',
                            stopped_at=now(),
                            updated_at=now()
                        WHERE id=:id
                        """
                    ),
                    {"id": cid},
                )
                stopped_ids.append(cid)
                continue

            if past_deadline and pid is not None:
                result = stop_capture(pid)
                await session.execute(
                    text(
                        """
                        UPDATE capture_sessions
                        SET state='finished',
                            stopped_at=now(),
                            updated_at=now(),
                            error=CASE WHEN :e = '' THEN error ELSE :e END
                        WHERE id=:id
                        """
                    ),
                    {
                        "id": cid,
                        "e": "" if result.get("status") in (
                            "terminated", "already_exited", "terminated_late"
                        ) else str(result),
                    },
                )
                stopped_ids.append(cid)
                logger.info(
                    "[capture-trigger] stopped cid=%s pid=%s result=%s",
                    cid, pid, result,
                )

        await session.commit()
    return stopped_ids


async def tick() -> dict[str, Any]:
    """One full pass — called by the scheduler."""
    scheduled = await schedule_upcoming()
    started = await start_due()
    stopped = await stop_overdue()
    return {
        "scheduled": scheduled,
        "started": started,
        "stopped": stopped,
    }
