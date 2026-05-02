"""
Saturday meta-review — Astra's weekly self-audit.

Uses the same runner machinery as a regular topic, but with:
  * depth="deep" (more findings, more build/subtract recs, Opus-class tokens)
  * Extra context: last 7 days of briefings, completed tasks, meetings,
    catchup applies, calendar proposal applies, commits.
  * Topic + focus framed as "what to build, what to subtract — now".

This is the briefing that should most directly change Kunal's roadmap.
If the rest of the week's briefings are intel, this one is direction.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from astra.db.engine import async_session
from astra.research.runner import run_research

logger = logging.getLogger(__name__)


async def gather_weekly_delta() -> dict[str, Any]:
    """Pull the last 7 days of *outcomes* — what shipped, what stalled."""
    since = datetime.now(timezone.utc) - timedelta(days=7)
    out: dict[str, Any] = {}

    async with async_session() as s:
        # Briefings this week
        r = await s.execute(
            text(
                """
                SELECT id, topic, status, LEFT(body_md, 400) FROM research_briefings
                WHERE created_at >= :since
                ORDER BY created_at DESC
                """
            ),
            {"since": since},
        )
        out["briefings_7d"] = [
            {"id": row[0], "topic": row[1], "status": row[2], "head": row[3]}
            for row in r.all()
        ]

        # Tasks completed this week
        r = await s.execute(
            text(
                """
                SELECT id, title, priority, tags, source, completed_at
                FROM tasks
                WHERE completed_at >= :since
                ORDER BY completed_at DESC
                LIMIT 30
                """
            ),
            {"since": since},
        )
        out["tasks_completed_7d"] = [
            {
                "id": row[0], "title": row[1], "priority": row[2],
                "tags": row[3], "source": row[4],
                "completed_at": row[5].isoformat() if row[5] else None,
            }
            for row in r.all()
        ]

        # Stalled: open tasks > 7 days old with no due or overdue
        r = await s.execute(
            text(
                """
                SELECT id, title, priority, created_at, due_at
                FROM tasks
                WHERE status = 'open'
                  AND created_at < now() - interval '7 days'
                ORDER BY priority DESC, created_at ASC
                LIMIT 20
                """
            )
        )
        out["stalled_tasks"] = [
            {
                "id": row[0], "title": row[1], "priority": row[2],
                "age_days": (datetime.now(timezone.utc) - row[3]).days if row[3] else None,
                "due_at": row[4].isoformat() if row[4] else None,
            }
            for row in r.all()
        ]

        # Meetings this week
        r = await s.execute(
            text(
                """
                SELECT id, title, duration_seconds,
                       jsonb_array_length(task_ids),
                       LEFT(summary, 300)
                FROM meetings
                WHERE created_at >= :since AND state = 'ready'
                ORDER BY created_at DESC
                LIMIT 20
                """
            ),
            {"since": since},
        )
        out["meetings_7d"] = [
            {
                "id": row[0], "title": row[1], "duration_s": row[2],
                "action_count": row[3], "summary_head": row[4],
            }
            for row in r.all()
        ]

        # Catchup applies + rejections this week
        r = await s.execute(
            text(
                """
                SELECT status, COUNT(*) FROM catchup_approvals
                WHERE created_at >= :since GROUP BY status
                """
            ),
            {"since": since},
        )
        out["catchup_week_by_status"] = {row[0]: row[1] for row in r.all()}

        # Calendar proposals applied / rejected
        r = await s.execute(
            text(
                """
                SELECT status, COUNT(*) FROM calendar_event_proposals
                WHERE created_at >= :since GROUP BY status
                """
            ),
            {"since": since},
        )
        out["calendar_proposals_week_by_status"] = {row[0]: row[1] for row in r.all()}

    return out


async def run_meta_review() -> dict[str, Any]:
    """Saturday 07:00 IST — the weekly self-audit briefing."""
    delta = await gather_weekly_delta()

    # Rich focus prompt — the weekly-delta is rendered into the topic
    # focus so the model sees it alongside the normal compass + state.
    import json as _j

    focus = (
        "SATURDAY META-REVIEW. Do a ruthless self-audit of Astra and "
        "Kunal's week. Use the compass + Astra state as usual, plus "
        "the weekly delta below.\n\n"
        "Answer four questions explicitly inside findings / build / "
        "subtract / urgencies:\n"
        "  1. What compass vector ADVANCED this week? (which business/"
        "ambition moved, by how much?)\n"
        "  2. What STALLED or got abandoned?\n"
        "  3. What should Astra BUILD next week — with compass-tie "
        "and priority?\n"
        "  4. What should Astra SUBTRACT — dormant code, stale "
        "features, redundant commitments?\n\n"
        "Be ruthless. If a feature has no usage signal, recommend "
        "subtracting it even if it took work to build. If a task has "
        "been stalled > 14 days with no progress, recommend "
        "subtracting it. Kunal's attention is the scarcest resource.\n\n"
        f"<weekly_delta>\n{_j.dumps(delta, indent=2, default=str)[:9000]}\n</weekly_delta>"
    )

    return await run_research(
        topic="Astra meta-review — what to build, what to subtract",
        topic_slug="astra-meta-review",
        prompt_focus=focus,
        business_tags="meta",
        kind="scheduled",
        depth="deep",
    )
