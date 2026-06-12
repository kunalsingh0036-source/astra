"""
Astra scheduler — APScheduler replacement for Celery Beat.

Why APScheduler (over Celery / Temporal / systemd timers):
  - Pure Python, no C extensions → immune to the billiard bug that
    breaks Celery on Python 3.14.
  - In-process with our async stack → jobs share the same event loop
    as the main app, no IPC overhead.
  - Jobstore in Postgres via SQLAlchemy → survives restart, state is
    transparent (just a table).
  - Single-file runtime — no worker/beat split to manage.

The scheduler can be run standalone (`python -m astra.scheduler.app`)
or started embedded in any other entry point by calling `start_scheduler()`.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from astra.config import settings
from astra.scheduler.jobs import (
    run_morning_briefing,
    run_evening_briefing,
    run_fleet_health_check,
    run_memory_consolidation,
    run_gmail_watch_renew,
    run_cost_report,
    run_notes_sync,
    run_missed_session_snapshot,
    run_training_catchup_prompt,
    run_apply_approved_catchups,
    run_calendar_sync,
    run_apply_approved_events,
    run_meetings_pipeline,
    run_meeting_capture_trigger,
    run_daily_research,
    run_inbox_preview,
    run_classify_sweep,
    run_classify_sweep_light,
    run_shares_pipeline,
    run_scheduler_self_check,
    run_betterstack_heartbeat,
    run_email_sync,
    run_retention_sweep,
    run_wa_dispatch,
    run_inbox_triage,
    run_weekly_review,
)

logger = logging.getLogger(__name__)

# Single module-level scheduler so the same instance is reused across
# import points. `start_scheduler` is idempotent.
_scheduler: Optional[AsyncIOScheduler] = None


def _build_scheduler() -> AsyncIOScheduler:
    """Construct the scheduler and register all jobs.

    Jobs are registered once, on startup. They run in the scheduler's
    event loop; each is async and defensive (see astra.scheduler.jobs).
    """
    # SQLAlchemy jobstore on the same Postgres Astra already uses.
    # Persisting jobs makes the scheduler introspectable from any
    # other process (alerts, /api/scheduler/state) and survives
    # restarts without losing the next-fire times.
    #
    # Important: APScheduler's SQLAlchemy jobstore needs a SYNC URL,
    # not asyncpg. Convert "postgresql+asyncpg://" → "postgresql://".
    sync_db_url = settings.database_url.replace("+asyncpg", "")
    jobstore = SQLAlchemyJobStore(url=sync_db_url, tablename="astra_scheduler_jobs")

    scheduler = AsyncIOScheduler(
        timezone="Asia/Kolkata",
        jobstores={"default": jobstore},
        job_defaults={
            # Coalesce a missed fire (e.g. laptop was asleep) — run once,
            # not every missed slot.
            "coalesce": True,
            # Don't pile up duplicates if a job overruns.
            "max_instances": 1,
            # Tolerate up to 5 min of schedule drift.
            "misfire_grace_time": 300,
        },
    )

    # Morning briefing — daily at the configured time (default 7:30 IST)
    scheduler.add_job(
        run_morning_briefing,
        CronTrigger(
            hour=settings.briefing_hour,
            minute=settings.briefing_minute,
        ),
        id="morning_briefing",
        name="Morning briefing",
        replace_existing=True,
    )

    # Evening briefing — 22:00 IST daily.
    # Two sections: "what we did today" + "what we're setting out to achieve
    # tomorrow". Lands just as Kunal gets home from evening training.
    # Emailed to kunalsingh0036@gmail.com and filed as episodic memory.
    scheduler.add_job(
        run_evening_briefing,
        CronTrigger(hour=22, minute=0),
        id="evening_briefing",
        name="Evening briefing",
        replace_existing=True,
    )

    # Fleet health check — every N seconds (default 300)
    scheduler.add_job(
        run_fleet_health_check,
        IntervalTrigger(seconds=settings.health_check_interval_seconds),
        id="fleet_health_check",
        name="Fleet health check",
        replace_existing=True,
    )

    # Gmail ingestion — every 5 min. POSTs the email agent's
    # /api/v1/sync (mesh-auth). Replaces the celery-beat path that
    # was never deployed to Railway; this is what actually fills the
    # message store the digests/briefings/classify sweeps read.
    scheduler.add_job(
        run_email_sync,
        IntervalTrigger(minutes=5),
        id="email_sync",
        name="Gmail ingestion (via email agent)",
        replace_existing=True,
    )

    # Retention sweep — daily 03:30 IST (off-peak). Windows approved
    # 2026-06-11: turn_events 30d, bridge_calls 14d, previews per-row
    # TTL (finally calling sweep_expired), turns.messages forever.
    scheduler.add_job(
        run_retention_sweep,
        CronTrigger(hour=3, minute=30),
        id="retention_sweep",
        name="Retention sweep (turn_events/bridge_calls/previews)",
        replace_existing=True,
    )

    # WhatsApp outbound drain — every 60s. Sends QUEUED gateway
    # messages via POST /api/v1/queue/drain (mesh-auth). Replaces the
    # celery-beat dispatch that was never deployed.
    scheduler.add_job(
        run_wa_dispatch,
        IntervalTrigger(seconds=60),
        id="wa_dispatch",
        name="WhatsApp outbound drain",
        replace_existing=True,
    )

    # Silent inbox triage — 12:15 IST, ahead of the operating-mode
    # 13:00 "silent triage done" bar. Stages reply drafts for
    # action-needed mail; the morning briefing (07:30) reports
    # yesterday's leftovers, this catches the morning's arrivals.
    scheduler.add_job(
        run_inbox_triage,
        CronTrigger(hour=12, minute=15),
        id="inbox_triage",
        name="Silent inbox triage (staged drafts)",
        replace_existing=True,
    )

    # Weekly cross-business review — Sunday 21:00 IST, before the
    # evening briefing. The compass question answered proactively:
    # where is attention/money leaking across the four businesses.
    scheduler.add_job(
        run_weekly_review,
        CronTrigger(day_of_week="sun", hour=21, minute=0),
        id="weekly_review",
        name="Weekly cross-business review",
        replace_existing=True,
    )

    # Scheduler self-check — every 5 min. Watches its own jobstore
    # for paused (NULL next_run) or overdue (>30 min late) jobs and
    # alerts via macOS + Web Push if anything's stuck. Closes the
    # loop on the alerting requirement from task #31.
    scheduler.add_job(
        run_scheduler_self_check,
        IntervalTrigger(minutes=5),
        id="scheduler_self_check",
        name="Scheduler self-check (jobstore health)",
        replace_existing=True,
    )

    # BetterStack heartbeat — every 5 min. External-watcher ping.
    # Internal self-check can't detect a dead scheduler (a dead
    # process can't fire its own alert). BetterStack pages Kunal if
    # this ping is missing for 5 min + 10 min grace.
    scheduler.add_job(
        run_betterstack_heartbeat,
        IntervalTrigger(minutes=5),
        id="betterstack_heartbeat",
        name="BetterStack uptime heartbeat",
        replace_existing=True,
    )

    # Memory consolidation — nightly (default 03:00 IST)
    scheduler.add_job(
        run_memory_consolidation,
        CronTrigger(hour=settings.consolidation_hour, minute=0),
        id="memory_consolidation",
        name="Memory consolidation",
        replace_existing=True,
    )

    # Gmail watch renewal — every 6 days at 02:17 IST (odd minute to
    # dodge :00 congestion, 6d < 7d watch expiry)
    scheduler.add_job(
        run_gmail_watch_renew,
        CronTrigger(hour=2, minute=17, day="*/6"),
        id="gmail_watch_renew",
        name="Gmail watch renewal",
        replace_existing=True,
    )

    # Cost report — weekly Monday 09:03 IST
    scheduler.add_job(
        run_cost_report,
        CronTrigger(day_of_week="mon", hour=9, minute=3),
        id="cost_report",
        name="Weekly cost report",
        replace_existing=True,
    )

    # ── macOS-only jobs ─────────────────────────────────────
    # These five shell out to /usr/bin/osascript (Apple Notes, macOS
    # notifications) or watch laptop-filesystem paths (~/Astra/
    # recordings). Registering them on the Linux cloud scheduler
    # meant they fired forever, failed forever, and — worse — the
    # apply-workers could NEVER apply approved rows, silently
    # stranding Kunal's /tonight submissions. Gate on the platform:
    # the cloud scheduler simply doesn't get them; a laptop-side
    # scheduler (if/when one runs again) registers them as before.
    import sys as _sys

    _IS_MACOS = _sys.platform == "darwin"
    if not _IS_MACOS:
        logger.info(
            "[scheduler] skipping 5 macOS-only jobs (notes_sync, "
            "missed_session_snapshot, training_catchup_prompt*, "
            "apply_approved_catchups, meetings_pipeline, "
            "meeting_capture_trigger) — no osascript on %s. "
            "*catchup prompt still posts the web notification path.",
            _sys.platform,
        )

    if _IS_MACOS:
        # Apple Notes sync — every 30 minutes. Incremental (only
        # changed notes re-fetched), so a no-op run is <2s.
        scheduler.add_job(
            run_notes_sync,
            IntervalTrigger(minutes=30),
            id="notes_sync",
            name="Apple Notes sync",
            replace_existing=True,
        )

        # Missed-session snapshot — daily at 21:30 IST, half an hour
        # before the evening briefing so the briefing reads a fresh
        # snapshot. Requires the Apple Notes mirror.
        scheduler.add_job(
            run_missed_session_snapshot,
            CronTrigger(hour=21, minute=30),
            id="missed_session_snapshot",
            name="Missed-session snapshot",
            replace_existing=True,
        )

        # Apply-worker — every 60s. Writes approved catchup rows to
        # the Kunal Apple Note via osascript.
        scheduler.add_job(
            run_apply_approved_catchups,
            IntervalTrigger(seconds=60),
            id="apply_approved_catchups",
            name="Apply approved catchups",
            replace_existing=True,
        )

    # Training catch-up prompt — 21:30 IST. Notification primary,
    # email secondary; BOTH legs work from the cloud (web push +
    # email-agent send), so this job stays cross-platform. Only the
    # Apple-Note writeback (apply-worker above) is macOS-bound.
    scheduler.add_job(
        run_training_catchup_prompt,
        CronTrigger(hour=21, minute=30),
        id="training_catchup_prompt",
        name="Training catch-up prompt",
        replace_existing=True,
    )

    # Google Calendar sync — every 10 minutes. Pulls the rolling
    # 14-day window (next 14 days + last 2h) into calendar_events.
    # First run requires Kunal to complete the OAuth consent flow
    # (browser opens, he clicks Allow). Subsequent runs auto-refresh.
    scheduler.add_job(
        run_calendar_sync,
        IntervalTrigger(minutes=10),
        id="calendar_sync",
        name="Google Calendar sync",
        replace_existing=True,
    )

    # Calendar apply-worker — every 60 s. Picks up approved proposals
    # and performs the Google Calendar API call (create/update/delete).
    # Expires pending proposals after 48h.
    scheduler.add_job(
        run_apply_approved_events,
        IntervalTrigger(seconds=60),
        id="apply_approved_events",
        name="Apply approved calendar events",
        replace_existing=True,
    )

    if _IS_MACOS:
        # Meetings pipeline — every 30 s. Scans ~/Astra/recordings
        # for dropped audio, transcribes via whisper.cpp, summarizes
        # via Claude, fires a macOS notification. Laptop-only by
        # nature (recordings land on the laptop filesystem).
        scheduler.add_job(
            run_meetings_pipeline,
            IntervalTrigger(seconds=30),
            id="meetings_pipeline",
            name="Meeting pipeline (transcribe + summarize)",
            replace_existing=True,
        )

        # Calendar-triggered auto-capture — every 60 s. Starts the
        # astra-capture subprocess on the laptop for upcoming Meets.
        scheduler.add_job(
            run_meeting_capture_trigger,
            IntervalTrigger(seconds=60),
            id="meeting_capture_trigger",
            name="Auto-capture (calendar-triggered)",
            replace_existing=True,
        )

    # Research Intel — 07:00 IST daily. Rotating topic queue drives
    # Mon-Fri + Sun. Saturday is the weekly meta-review (self-audit
    # of Astra — what to build, what to subtract). Reads before the
    # 07:30 morning briefing so the briefing can fold in the top line.
    scheduler.add_job(
        run_daily_research,
        CronTrigger(hour=7, minute=0),
        id="daily_research",
        name="Research Intel (daily rotating topic + Sat meta-review)",
        replace_existing=True,
    )

    # Inbox preview — 12:45 IST, 15 min before Kunal's 13:00 work
    # window. Notifies the headline (unanswered + action-needed)
    # and files the digest as a memory for the evening briefing.
    scheduler.add_job(
        run_inbox_preview,
        CronTrigger(hour=12, minute=45),
        id="inbox_preview",
        name="Inbox preview (12:45 IST before work window)",
        replace_existing=True,
    )

    # Classifier pre-sweep — 12:40 IST, 5 min before the inbox_preview.
    # Up to 80 unclassified rows get category + priority + summary from
    # Haiku. Catches the bulk of overnight-to-noon mail so the notification
    # lands with a clean, categorised inbox.
    scheduler.add_job(
        run_classify_sweep,
        CronTrigger(hour=12, minute=40),
        id="classify_sweep",
        name="Email classifier sweep (pre-inbox-preview)",
        replace_existing=True,
    )

    # Classifier backstop — every 30 min at minute :07, 15 row
    # cap, retries disabled. Keeps the inbox categorised throughout
    # the day without re-burning tokens on rows that already failed
    # once (those get picked up only by the big 12:40 sweep).
    scheduler.add_job(
        run_classify_sweep_light,
        CronTrigger(minute="7,37"),
        id="classify_sweep_light",
        name="Email classifier backstop (every 30 min)",
        replace_existing=True,
    )

    # Shares pipeline — every 30s. Walks shares in state='received',
    # classifies each via Claude Haiku, routes to memory/task/meeting.
    scheduler.add_job(
        run_shares_pipeline,
        IntervalTrigger(seconds=30),
        id="shares_pipeline",
        name="iOS share sheet pipeline",
        replace_existing=True,
    )

    return scheduler


def start_scheduler() -> AsyncIOScheduler:
    """Start (or return) the singleton scheduler.

    Safe to call multiple times: only boots once. Returns the scheduler
    so callers can add ad-hoc jobs or inspect the job store.
    """
    global _scheduler
    if _scheduler is None:
        _scheduler = _build_scheduler()
    if not _scheduler.running:
        _scheduler.start()
        # The jobstore is Postgres-persisted: jobs registered by a
        # PREVIOUS process (e.g. the macOS-only set, before they were
        # platform-gated) survive in astra_scheduler_jobs and load on
        # start regardless of what _build_scheduler registered. Purge
        # any job that's persisted but platform-inappropriate here —
        # otherwise the gating above only affects fresh databases.
        import sys as _sys

        if _sys.platform != "darwin":
            _MACOS_JOB_IDS = (
                "notes_sync",
                "missed_session_snapshot",
                "apply_approved_catchups",
                "meetings_pipeline",
                "meeting_capture_trigger",
            )
            for _jid in _MACOS_JOB_IDS:
                try:
                    _scheduler.remove_job(_jid)
                    logger.info(
                        "[scheduler] purged persisted macOS-only job %r "
                        "(platform=%s)",
                        _jid,
                        _sys.platform,
                    )
                except Exception:
                    pass  # not in the store — nothing to purge
        logger.info(
            "[scheduler] started — jobs: %s",
            [j.id for j in _scheduler.get_jobs()],
        )
    return _scheduler


def shutdown_scheduler() -> None:
    """Stop the scheduler if running. Called on graceful shutdown."""
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("[scheduler] shutdown")


def list_jobs() -> list[dict]:
    """Return a snapshot of registered jobs — used by the service tools."""
    global _scheduler
    if _scheduler is None:
        return []
    return [
        {
            "id": j.id,
            "name": j.name,
            "next_run": j.next_run_time.isoformat() if j.next_run_time else None,
            "trigger": str(j.trigger),
        }
        for j in _scheduler.get_jobs()
    ]


async def _main_loop():
    """Standalone entry point — runs the scheduler until SIGINT/SIGTERM."""
    start_scheduler()

    stop = asyncio.Event()

    def _handle_signal(*_):
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except NotImplementedError:
            # Signals not available on some platforms (e.g. Windows)
            pass

    logger.info("[scheduler] running · ctrl-c to stop")
    await stop.wait()
    shutdown_scheduler()


def main() -> None:
    """Entrypoint for `python -m astra.scheduler.app`."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(_main_loop())


if __name__ == "__main__":
    main()
