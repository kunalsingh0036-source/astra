"""
Scheduled jobs for Astra — async implementations.

Pure async functions that APScheduler invokes directly. No Celery, no
subprocess pool, no C extensions — just Python coroutines on the same
event loop as the main app.

Each job is defensive: it catches all exceptions and logs rather than
crashing the scheduler. A single failing job must never break the rest.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


async def morning_briefing() -> dict:
    """Generate a daily status summary and store it as episodic memory.

    Folds in today's Research Intel briefing (fired 30 min earlier at
    07:00 IST) so the morning read shows the gist + top build /
    subtract / urgency lines without having to click through.
    """
    from astra.services.manager import service_manager
    from astra.memory.consolidation import get_memory_stats
    from astra.memory.store import store_memory
    from astra.memory.models import MemoryType
    from astra.db.engine import async_session
    from sqlalchemy import text
    from datetime import timedelta

    fleet = service_manager.status_all()
    running = [s for s in fleet if s["status"] == "running"]
    stopped = [s for s in fleet if s["status"] != "running"]

    async with async_session() as session:
        mem_stats = await get_memory_stats(session)

    # Today's research — most recent briefing created within last 3h.
    research_lines: list[str] = []
    try:
        since = datetime.now(timezone.utc) - timedelta(hours=3)
        async with async_session() as session:
            r = await session.execute(
                text(
                    """
                    SELECT id, topic, status, body_md
                    FROM research_briefings
                    WHERE created_at >= :since
                    ORDER BY created_at DESC
                    LIMIT 1
                    """
                ),
                {"since": since},
            )
            row = r.first()
        if row and row[2] == "ready":
            research_lines = [
                "",
                f"Research Intel #{row[0]} — {row[1]}",
                _extract_gist_and_top(row[3]),
            ]
        elif row:
            research_lines = [
                "",
                f"Research Intel #{row[0]} — {row[1]} (status: {row[2]})",
            ]
    except Exception as e:
        logger.warning("[morning] research fold-in failed: %s", e)

    now = datetime.now(timezone.utc)
    lines = [
        f"Morning Briefing — {now.strftime('%A, %B %d %Y')}",
        "",
        f"Fleet: {len(running)}/{len(fleet)} services running",
    ]
    if stopped:
        lines.append(f"  Stopped: {', '.join(s['name'] for s in stopped)}")
    lines.extend([
        "",
        f"Memory: {mem_stats['total_memories']} memories stored",
        f"  Types: {mem_stats['by_type']}",
        f"  Avg importance: {mem_stats['avg_importance']}",
    ])
    lines.extend(research_lines)

    briefing_text = "\n".join(lines)
    logger.info("[scheduler] morning_briefing: %s", briefing_text.replace("\n", " | "))

    async with async_session() as session:
        await store_memory(
            session=session,
            content=briefing_text,
            memory_type=MemoryType.EPISODIC,
            source="scheduler",
            tags="briefing,daily,proactive",
            importance=0.4,
        )

    return {"status": "success", "briefing": briefing_text}


def _extract_gist_and_top(body_md: str) -> str:
    """Pull the gist line + first bullet from Build/Subtract/Urgent
    sections. Graceful fallback to first 400 chars."""
    if not body_md:
        return ""
    gist = ""
    blocks: dict[str, list[str]] = {"Build": [], "Subtract": [], "Urgent": []}
    current: str | None = None
    for line in body_md.splitlines():
        if line.startswith("**Gist.**"):
            gist = line.replace("**Gist.**", "").strip()
            continue
        stripped = line.strip()
        for header in blocks.keys():
            if stripped == f"## {header}":
                current = header
                break
        else:
            if stripped.startswith("## "):
                current = None
            elif current and stripped.startswith("- ") and len(blocks[current]) < 1:
                blocks[current].append(stripped[2:])
    out: list[str] = []
    if gist:
        out.append(gist)
    for h in ("Urgent", "Build", "Subtract"):
        if blocks[h]:
            out.append(f"  {h.lower()}: {blocks[h][0][:160]}")
    return "\n".join(out) if out else body_md[:400]


async def scheduler_self_check() -> dict:
    """Every 5 min — verify the scheduler's own jobstore is healthy.

    Reads `astra_scheduler_jobs` directly. Alerts if any job has a
    NULL next_run_time (paused / never scheduled) or a next_run_time
    more than 2× its interval in the past (stuck).

    Closes the loop on tasks #19 / #30 / #31 (alerting requirement).
    """
    from sqlalchemy import text as _text
    from astra.db.engine import async_session
    from astra.notifications import notify
    import datetime as _dt

    issues: list[str] = []
    async with async_session() as s:
        # Detect missing jobs (jobstore should have ~19; threshold 15)
        r = await s.execute(_text(
            "SELECT id, next_run_time FROM astra_scheduler_jobs ORDER BY id"
        ))
        rows = r.all()
    if not rows:
        msg = "scheduler jobstore empty — scheduler likely down"
        issues.append(msg)
    else:
        now_ts = _dt.datetime.now(_dt.timezone.utc).timestamp()
        for jid, nrt in rows:
            if nrt is None:
                issues.append(f"{jid}: next_run_time NULL (paused)")
            elif nrt < now_ts - 1800:  # 30 min past due
                overdue_min = (now_ts - nrt) / 60
                issues.append(f"{jid}: overdue by {overdue_min:.0f}m")

    if issues:
        body = "Scheduler health alert:\n" + "\n".join(f"  · {i}" for i in issues[:8])
        logger.warning("[scheduler-self-check] %s", body.replace("\n", " | "))
        # IMPORTANT: this is operational TELEMETRY, not knowledge. It used
        # to be written as an importance-0.75 EPISODIC memory every 5 min,
        # which (a) flooded the memory store with near-identical rows and
        # (b) got recalled into briefings/chat as a standing "scheduler is
        # dead two weeks" crisis long after any transient issue cleared —
        # a stale snapshot (e.g. captured mid-deploy) haunting every brief.
        # Alert via push + log only; the brief/chat read scheduler health
        # LIVE, never from recalled memory. (2026-06-13 confabulation audit.)
        notify(
            title="astra · scheduler issue",
            body=f"{len(issues)} scheduler problem(s) — see /today",
            url="/today",
            tag="scheduler-alert",
        )
        return {"status": "alert", "issues": issues}
    return {"status": "ok", "job_count": len(rows)}


async def run_scheduler_self_check():
    return await _safe("scheduler_self_check", scheduler_self_check)


# ── External uptime heartbeat (BetterStack) ───────────────────────


async def betterstack_heartbeat() -> dict:
    """Ping BetterStack's heartbeat URL so an external watcher knows
    the scheduler is alive.

    Why an external watcher: scheduler_self_check is internal (it runs
    inside the scheduler's own process). If the scheduler is dead, it
    can't tell anyone. BetterStack's heartbeat model inverts that:
    BetterStack expects a periodic GET; if N+grace seconds pass without
    one, it pages Kunal. So a dead scheduler = a missed ping = an alert.

    Configured via the BETTERSTACK_HEARTBEAT_URL env var; if unset,
    this job is a silent no-op (lets dev mode run without a paid
    monitoring relationship).
    """
    import os

    url = os.environ.get("BETTERSTACK_HEARTBEAT_URL", "").strip()
    if not url:
        return {"status": "skipped", "reason": "BETTERSTACK_HEARTBEAT_URL not set"}

    # GATE THE PING ON REAL SYSTEM HEALTH, not just "this process is alive".
    # On 2026-06-15 Postgres died (disk-full → read-only) for ~11h, but this
    # job kept pinging BetterStack — the GET touches no DB — so BetterStack
    # saw a healthy heartbeat and never alerted. A liveness ping is not a
    # health check. Now: prove the DB is reachable (SELECT 1) FIRST; if it
    # isn't, DON'T ping, so the missed heartbeat makes BetterStack page Kunal.
    try:
        from sqlalchemy import text as _t

        from astra.db.engine import async_session

        async with async_session() as s:
            await s.execute(_t("SELECT 1"))
    except Exception as e:
        logger.error(
            "[heartbeat] DB unreachable — SUPPRESSING ping so BetterStack "
            "alerts on the missed heartbeat: %s",
            e,
        )
        return {"status": "suppressed-db-down", "error": str(e)[:200]}

    try:
        import httpx

        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url)
            r.raise_for_status()
        return {"status": "pinged", "code": r.status_code}
    except Exception as e:
        # Don't raise — a heartbeat ping that fails should not crash the
        # scheduler. The whole point is to detect THAT crash separately.
        logger.warning("[scheduler] betterstack heartbeat failed: %s", e)
        return {"status": "error", "error": str(e)[:200]}


async def run_betterstack_heartbeat():
    return await _safe("betterstack_heartbeat", betterstack_heartbeat)


async def _cloud_fleet_probe() -> list[dict]:
    """Probe the DEPLOYED fleet over Railway's private network.

    The legacy path (service_manager.health_check_all) probes laptop
    localhost ports and working directories that were decommissioned
    in the Railway migration — on the cloud scheduler it reported
    0/9 running forever, filing 'fleet health issue' memories about a
    fleet that doesn't exist. This is the honest replacement.

    Override targets with FLEET_HEALTH_URLS (comma-separated
    name=url pairs) if the topology changes.
    """
    import os

    import httpx

    raw = os.environ.get("FLEET_HEALTH_URLS", "").strip()
    if raw:
        targets = {}
        for pair in raw.split(","):
            name, _, url = pair.strip().partition("=")
            if name and url:
                targets[name] = url
    else:
        # Only ALWAYS-ON cloud services belong here. The old list probed
        # finance.railway.internal + bridge.railway.internal — both DELETED
        # in the R3 consolidation (folded into `agents`) — so the alerter
        # paged on two permanently-dead hostnames the moment it was revived
        # (2026-07-03). The Mac bridge daemon is deliberately NOT probed:
        # laptop closed = its NORMAL state, not an incident (Kunal's rule:
        # never ping "bridge is down"; explain at the point an action
        # actually needs the Mac).
        targets = {
            "stream": "http://stream.railway.internal:8080/health",
            "email": "http://email.railway.internal:8080/health",
            "whatsapp": "http://whatsapp.railway.internal:8080/health",
            "agents": "http://agents.railway.internal:8080/health",
        }

    results: list[dict] = []
    async with httpx.AsyncClient(timeout=5.0) as client:
        for name, url in targets.items():
            try:
                r = await client.get(url)
                results.append(
                    {
                        "service": name,
                        "status": "healthy" if r.status_code == 200 else "unhealthy",
                    }
                )
            except Exception:
                results.append({"service": name, "status": "unhealthy"})
    return results


# ── Fleet health monitor state ──────────────────────────────────────
# In-process (resets on scheduler restart — fine, a restart re-evaluates).
# Tracks consecutive failures per service and which we've already alerted
# on, so we ping ONCE on a sustained transition to down and ONCE on
# recovery — never every cycle (the cry-wolf bug).
_FLEET_FAILS: dict[str, int] = {}
_FLEET_ALERTED: set[str] = set()
_FLEET_SUSTAINED = 2  # consecutive unhealthy probes before it's "real"
_FLEET_PURGED = False  # one-shot stale-telemetry purge guard


async def _notify_owner_fleet(text: str) -> None:
    """Loud owner ping for a REAL fleet transition (down/recovery).
    WhatsApp + push; best-effort. The ONLY channel fleet health uses —
    no memory writes (see fleet_health_check)."""
    import os

    import httpx

    try:
        gw = os.environ.get(
            "GATEWAY_URL", "http://whatsapp.railway.internal:8080"
        ).rstrip("/")
        headers = {
            "x-astra-secret": os.environ.get("AGENT_SHARED_SECRET", "").strip()
        }
        async with httpx.AsyncClient(timeout=30.0) as c:
            await c.post(
                f"{gw}/api/v1/notify/owner", json={"text": text}, headers=headers
            )
    except Exception as e:
        logger.info("[fleet_health] WA notify skipped: %s", e)
    try:
        from astra.notifications import notify

        notify(title="astra · fleet", body=text[:120], url="/today",
               tag="fleet-health", also_push=True)
    except Exception as e:
        logger.info("[fleet_health] push skipped: %s", e)


async def fleet_health_check() -> dict:
    """Probe the deployed fleet; alert ONLY on a sustained, NEW failure
    (and on recovery). Silent when healthy.

    Critically, NEVER writes memory. Health is LIVE state, not knowledge —
    the old version stored 'X unhealthy' as importance-0.7 episodic memory
    every cycle, which got recalled into briefs/chat and replayed transient
    blips (deploys, the bridge build outage) as a standing 'X down' crisis
    long after recovery. That's the SAME confabulation bug scheduler_self_
    check was already fixed for (2026-06-13); fleet_health_check was the
    missed instance. Brief/chat read fleet health LIVE via _fleet_line."""
    import sys as _sys

    # _FLEET_ALERTED is REBOUND below (|= / -=), so it must be declared
    # global or Python treats it as local and the first read raises
    # UnboundLocalError — which killed every fleet_health_check run
    # (silently, inside _safe) from Jun 29 to Jul 2: down-alerts dead.
    global _FLEET_PURGED, _FLEET_ALERTED

    if _sys.platform == "darwin":
        from astra.services.manager import service_manager

        results = await service_manager.health_check_all()
    else:
        results = await _cloud_fleet_probe()

    # One-shot: purge the stale fleet/scheduler telemetry memories that have
    # been polluting briefs as phantom "down" pings. Pure telemetry, never
    # knowledge — safe to delete. After this runs once the table stays clean
    # because we no longer write them.
    if not _FLEET_PURGED:
        try:
            from sqlalchemy import text as _text

            from astra.db.engine import async_session

            async with async_session() as s:
                r = await s.execute(
                    _text(
                        "DELETE FROM memories WHERE source = 'scheduler' "
                        "AND (tags LIKE '%fleet%' OR tags LIKE '%alert%')"
                    )
                )
                await s.commit()
                logger.info(
                    "[fleet_health] purged %s stale telemetry memories",
                    r.rowcount,
                )
        except Exception as e:
            logger.warning("[fleet_health] telemetry purge skipped: %s", e)
        _FLEET_PURGED = True

    healthy = {r["service"] for r in results if r["status"] == "healthy"}
    unhealthy = {r["service"] for r in results if r["status"] == "unhealthy"}
    for svc in healthy:
        _FLEET_FAILS[svc] = 0
    for svc in unhealthy:
        _FLEET_FAILS[svc] = _FLEET_FAILS.get(svc, 0) + 1

    sustained = {s for s in unhealthy if _FLEET_FAILS[s] >= _FLEET_SUSTAINED}
    new_down = sustained - _FLEET_ALERTED
    recovered = {s for s in _FLEET_ALERTED if _FLEET_FAILS.get(s, 0) == 0}

    if new_down:
        names = ", ".join(sorted(new_down))
        logger.error("[fleet_health] SUSTAINED DOWN → %s", names)
        await _notify_owner_fleet(
            f"🔴 Service down: {names} — unreachable {_FLEET_SUSTAINED}+ checks. "
            "Astra ops degraded; check /today."
        )
        _FLEET_ALERTED |= new_down
    if recovered:
        names = ", ".join(sorted(recovered))
        logger.info("[fleet_health] recovered → %s", names)
        await _notify_owner_fleet(f"✅ Recovered: {names}.")
        _FLEET_ALERTED -= recovered

    if not unhealthy:
        logger.info("[fleet_health] all %d healthy", len(healthy))

    return {
        "healthy": len(healthy),
        "unhealthy": len(unhealthy),
        "total": len(results),
        "alerted": sorted(_FLEET_ALERTED),
    }


async def memory_consolidation() -> dict:
    """Nightly: prune, decay, merge, summarize old memories."""
    from astra.db.engine import async_session
    from astra.memory.consolidation import run_full_consolidation

    async with async_session() as session:
        report = await run_full_consolidation(session)

    logger.info("[scheduler] consolidation: %s", report)
    return {"status": "success", "report": report}


async def gmail_watch_renew() -> dict:
    """Renew Gmail push notification watch before it expires (7d cycle)."""
    import httpx

    # email-agent exposes the watch renewal at /api/v1/webhook/gmail/watch.
    # Canonical base + mesh secret from astra/email/client.py — the
    # hardcoded localhost this replaced is the bug that silently
    # severed every scheduler→email-agent call after the Railway
    # migration.
    from astra.email.client import BASE_URL as _email_base, mesh_headers

    email_url = f"{_email_base}/api/v1/webhook/gmail/watch"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(email_url, headers=mesh_headers())
            if resp.status_code != 200:
                logger.warning("[scheduler] gmail_watch_renew failed: %s", resp.status_code)
                return {"status": "error", "code": resp.status_code}
            body = resp.json()
            logger.info("[scheduler] gmail_watch_renew: %s", body)
            return {"status": "success", "body": body}
    except Exception as e:
        logger.error("[scheduler] gmail_watch_renew error: %s", e)
        return {"status": "error", "error": str(e)}


async def cost_report() -> dict:
    """Weekly: write a cost-tracking memo. Placeholder until usage logging lands."""
    from astra.db.engine import async_session
    from astra.memory.store import store_memory
    from astra.memory.models import MemoryType

    now = datetime.now(timezone.utc)
    text = (
        f"Weekly Cost Report — {now.strftime('%B %d, %Y')}\n"
        f"Cost tracking not yet implemented. Will track per-model token "
        f"usage from the audit log."
    )

    async with async_session() as session:
        await store_memory(
            session=session,
            content=text,
            memory_type=MemoryType.EPISODIC,
            source="scheduler",
            tags="cost,weekly,report",
            importance=0.3,
        )

    return {"status": "success", "report": text}


async def notes_sync() -> dict:
    """Pull the latest from Apple Notes into the `apple_notes` mirror.

    Incremental: only notes whose modification date changed are
    re-fetched. Typical run: <2s for no-op, 10–30s on a full re-sync.
    """
    from astra.notes.harvester import sync_all

    report = await sync_all(force=False)
    logger.info(
        "[scheduler] notes_sync: %d seen, +%d new, ~%d updated in %dms",
        report.total_notes_seen,
        report.new_notes,
        report.updated_notes,
        report.elapsed_ms,
    )
    return {
        "status": "success",
        "seen": report.total_notes_seen,
        "new": report.new_notes,
        "updated": report.updated_notes,
        "failed": report.failed_notes,
        "elapsed_ms": report.elapsed_ms,
    }


async def missed_session_snapshot() -> dict:
    """Daily snapshot of Kunal's missed-session counters from the
    "Kunal" Apple Note.

    Idempotent per UTC day — re-runs update the same row, so a
    mid-day catch-up after Saturday training reflects in the debt
    numbers without waiting for tomorrow. After ~7 consecutive runs
    the evening briefing gains a real week-over-week trendline.
    """
    from astra.notes.harvester import sync_all
    from astra.notes.missed_sessions import snapshot_today

    # Make sure the note mirror is fresh before parsing.
    try:
        await sync_all(force=False)
    except Exception as e:
        logger.warning("[scheduler] notes pre-sync failed: %s", e)

    result = await snapshot_today()
    logger.info("[scheduler] missed_session_snapshot: %s", result)
    return result


async def evening_briefing() -> dict:
    """22:00 IST daily briefing — the two-section end-of-day report.

    Section 1: what we did today (measured against Kunal's compass).
    Section 2: what we're setting out to achieve tomorrow.

    Data sources:
      - Gmail: today's sent + received (via email-agent)
      - Tasks: completed + opened today (via astra DB)
      - Agent activity: usage_events turns today
      - Memory: anything important filed today
      - Compass: loaded from ~/.claude/.../memory/kunal_compass.md

    Output:
      - Stored as episodic memory (tag: briefing,evening,proactive)
      - Emailed to kunalsingh0036@gmail.com
      - Available at /briefing in astra-web
    """
    from datetime import datetime, timedelta, timezone

    import httpx
    from sqlalchemy import text

    from astra.db.engine import async_session
    from astra.memory.models import MemoryType
    from astra.memory.store import store_memory

    ist = timezone(timedelta(hours=5, minutes=30))
    now_ist = datetime.now(ist)
    today_start_ist = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
    today_start_utc = today_start_ist.astimezone(timezone.utc)
    now_utc = datetime.now(timezone.utc)

    # ── gather: tasks ──────────────────────────────────────
    tasks_done: list[dict] = []
    tasks_open: list[dict] = []
    async with async_session() as session:
        rows = await session.execute(
            text(
                """
                SELECT id, title, note, status, priority, completed_at, created_at
                FROM tasks
                WHERE completed_at >= :since
                   OR (status = 'open' AND created_at >= :since)
                ORDER BY COALESCE(completed_at, created_at) DESC
                LIMIT 50
                """
            ),
            {"since": today_start_utc},
        )
        for r in rows.all():
            row = {
                "id": r[0],
                "title": r[1],
                "note": r[2] or "",
                "status": r[3],
                "priority": r[4],
                "completed_at": r[5].isoformat() if r[5] else None,
                "created_at": r[6].isoformat() if r[6] else None,
            }
            (tasks_done if row["status"] == "done" else tasks_open).append(row)

        # Also get open-high-priority tasks (regardless of age) so tomorrow
        # knows what's overdue / pending.
        rows = await session.execute(
            text(
                """
                SELECT id, title, priority, due_at, created_at
                FROM tasks
                WHERE status = 'open' AND priority >= 2
                ORDER BY COALESCE(due_at, created_at) ASC
                LIMIT 10
                """
            )
        )
        high_pri_open = [
            {
                "id": r[0],
                "title": r[1],
                "priority": r[2],
                "due_at": r[3].isoformat() if r[3] else None,
            }
            for r in rows.all()
        ]

        # ── gather: agent usage ──────────────────────────────
        usage_row = await session.execute(
            text(
                """
                SELECT COUNT(*), COALESCE(SUM(cost_usd), 0), COALESCE(SUM(input_tokens), 0),
                       COALESCE(SUM(output_tokens), 0)
                FROM usage_events WHERE ts >= :since
                """
            ),
            {"since": today_start_utc},
        )
        uc = usage_row.one()
        usage_today = {
            "turns": int(uc[0] or 0),
            "cost_usd": float(uc[1] or 0),
            "input_tokens": int(uc[2] or 0),
            "output_tokens": int(uc[3] or 0),
        }

        # ── gather: memories written today ──────────────────
        mem_rows = await session.execute(
            text(
                """
                SELECT content, memory_type::text, tags, importance
                FROM memories WHERE created_at >= :since
                ORDER BY importance DESC LIMIT 12
                """
            ),
            {"since": today_start_utc},
        )
        memories_today = [
            {
                "content": r[0],
                "type": r[1],
                "tags": r[2],
                "importance": float(r[3] or 0),
            }
            for r in mem_rows.all()
        ]

    # ── gather: email digest + unanswered ────────────────
    # Briefings need a real read on the inbox, not just a counter.
    # The astra.email module filters out noreply noise, surfaces
    # unanswered threads, and returns notable subjects with senders.
    email_summary: dict = {}
    try:
        from astra.email.signals import daily_digest, unanswered_incoming

        digest = await daily_digest(window_hours=24)
        unanswered = (await unanswered_incoming(days=14))[:8]
        email_summary = {
            "digest_24h": digest,
            "unanswered_14d_top8": unanswered,
        }
    except Exception as e:
        logger.warning("[scheduler] email signal gather failed: %s", e)
        # Fall back to the raw summary counter if the signal pipe
        # is down — better than nothing.
        try:
            from astra.email.client import BASE_URL as _email_base, mesh_headers

            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.get(
                    f"{_email_base}/api/v1/messages/summary",
                    headers=mesh_headers(),
                )
                if r.status_code == 200:
                    email_summary = {"raw_summary": r.json()}
        except Exception:
            pass

    # ── ingest catch-up reply BEFORE notes gather ──────────
    # The 21:30 prompt (astra · catch-up) asks Kunal what he got
    # done today. If he replied in the 30 minutes between 21:30 and
    # 22:00, ingest now so the writeback runs and the counters we
    # read below are already decremented. Idempotent by reply id.
    catchup_result: dict = {"status": "not_run"}
    try:
        from astra.scheduler.catchup import ingest_latest_reply

        catchup_result = await ingest_latest_reply()
        logger.info("[scheduler] evening catchup ingest: %s", catchup_result)
    except Exception as e:
        logger.warning("[scheduler] catchup ingest failed: %s", e)
        catchup_result = {"status": "error", "error": str(e)}

    # ── gather: Apple Notes signals ────────────────────────
    # The "Kunal" note is the training log; other recent edits
    # surface what Kunal has been thinking about today.
    notes_signal: dict = {
        "training_log_excerpt": "",
        "recent_edits": [],
        "missed_sessions": None,
        "catchup_tonight": catchup_result,
    }
    try:
        from astra.notes.store import list_notes, search_notes

        kunal_hits = await search_notes("Kunal", limit=1)
        if kunal_hits:
            notes_signal["training_log_excerpt"] = kunal_hits[0].get(
                "body_text", ""
            )[:800]

        # Notes edited today (based on modified_at_native).
        recent = await list_notes(limit=10, min_chars=20)
        edited_today = [
            n
            for n in recent
            if n.get("modified_at_native")
            and n["modified_at_native"] >= today_start_utc.isoformat()
        ]
        notes_signal["recent_edits"] = [
            {
                "title": n["title"],
                "folder": n["folder"],
                "chars": n["char_count"],
                "modified": n["modified_at_native"],
                "preview": (n.get("body_text") or "")[:240],
            }
            for n in edited_today
        ]

        # Missed-session trendline — today's debt + week-over-week
        # delta + direction per type. After ~7 daily snapshots the
        # direction dict has real "gap closed / gap grew / flat"
        # values; before then the briefing should say it's still
        # collecting baseline.
        try:
            from astra.notes.missed_sessions import trend, snapshot_today

            # Refresh today's row so this evening's catch-up training
            # is reflected before we diff.
            await snapshot_today()
            trend_data = await trend(days=14)
            notes_signal["missed_sessions"] = {
                "today": trend_data["today"],
                "week_ago": trend_data["week_ago"],
                "wow_delta": trend_data["wow_delta"],
                "direction": trend_data["direction"],
                "baseline_days_collected": len(trend_data["series"]),
            }
        except Exception as e:
            logger.warning("[scheduler] missed-session trend gather failed: %s", e)
    except Exception as e:
        logger.warning("[scheduler] notes signal gather failed: %s", e)

    # ── gather: calendar events (today + tomorrow) ────────
    # The briefing's core purpose is "tomorrow's game plan" — without
    # real calendar data we were flying blind on the work window.
    calendar_signal: dict = {
        "today_events": [],
        "tomorrow_events": [],
        "authorized": False,
    }
    try:
        from astra.calendar.client import is_authorized
        from astra.calendar.store import (
            list_events_today,
            list_events_tomorrow,
        )

        calendar_signal["authorized"] = is_authorized()
        if calendar_signal["authorized"]:
            today_ev = await list_events_today()
            tomorrow_ev = await list_events_tomorrow()
            # Trim for prompt length — drop description if long, keep
            # the things a briefing needs: title, time, attendees, meet.
            def _trim(ev: dict) -> dict:
                return {
                    "summary": ev.get("summary", ""),
                    "start_at": ev.get("start_at"),
                    "end_at": ev.get("end_at"),
                    "is_all_day": ev.get("is_all_day"),
                    "tz": ev.get("tz"),
                    "location": ev.get("location", ""),
                    "meet_link": ev.get("meet_link", ""),
                    "attendees": [
                        a.get("email") for a in (ev.get("attendees") or [])
                    ][:12],
                    "organizer_email": ev.get("organizer_email", ""),
                }

            calendar_signal["today_events"] = [_trim(e) for e in today_ev]
            calendar_signal["tomorrow_events"] = [_trim(e) for e in tomorrow_ev]
    except Exception as e:
        logger.warning("[scheduler] calendar signal gather failed: %s", e)

    # ── load compass from memory file ──────────────────────
    compass_text = ""
    try:
        from pathlib import Path

        compass_path = Path(
            "/Users/kunalsingh/.claude/projects/"
            "-Users-kunalsingh-Claude-Code/memory/kunal_compass.md"
        )
        if compass_path.exists():
            compass_text = compass_path.read_text()
    except Exception:
        pass

    # ── synthesize with Claude ─────────────────────────────
    # Resolve the API key robustly: Claude Code harness + `astra up`
    # can pass an empty ANTHROPIC_API_KEY down to child processes which
    # pydantic-settings then prefers over the .env file. Fall back to
    # reading .env directly when settings/env are both empty.
    import os as _os
    from pathlib import Path as _Path

    from astra.config import settings as astra_settings
    import anthropic

    api_key = astra_settings.anthropic_api_key or _os.environ.get(
        "ANTHROPIC_API_KEY", ""
    )
    if not api_key:
        try:
            env_path = _Path(__file__).resolve().parents[2] / ".env"
            if env_path.exists():
                for line in env_path.read_text().splitlines():
                    if line.startswith("ANTHROPIC_API_KEY="):
                        api_key = line.split("=", 1)[1].strip().strip('"').strip("'")
                        break
        except Exception:
            pass

    client = anthropic.AsyncAnthropic(api_key=api_key)

    signal = {
        "date_ist": now_ist.strftime("%A, %B %d %Y"),
        "tasks_done_today": tasks_done,
        "tasks_created_today": tasks_open,
        "open_high_priority": high_pri_open,
        "agent_activity": usage_today,
        "memories_written_today": memories_today,
        "email_summary": email_summary,
        "notes_signal": notes_signal,
        "calendar_signal": calendar_signal,
    }

    prompt = f"""You are Astra, Kunal's personal AI agent. Tonight is {now_ist.strftime('%A %d %b %Y')} at 22:00 IST.

Write Kunal's end-of-day briefing. Exactly two sections, italic-serif voice, short declarative sentences. Competent peer, not assistant. Lead with specifics.

SECTION 1 — "What we did today"
SECTION 2 — "What we're setting out to achieve tomorrow"

Measure against the compass below: every point should advance Kunal's three ambitions (top AI person, Olympic gold in squash, tech decision-maker for India) or one of the four businesses (HelmTech, Apex, Bay, Top Studios) or directly supports the interlocks between them.

Tomorrow's plan must respect Kunal's schedule:
- 05:30–13:00 and 18:00–20:30: training (untouchable)
- 13:00–18:00: the only work window for meetings
- Default meeting time: 13:30–17:30 IST

CALENDAR (`calendar_signal`):
  - If `authorized=false`: the calendar integration exists but Kunal
    hasn't completed the OAuth consent yet. Briefly note this at the
    end: "Calendar not connected — run the one-time consent when you
    get a minute so tomorrow's brief can see your schedule." Do NOT
    block the brief on this.
  - If `authorized=true`: `tomorrow_events` is a list of {{summary,
    start_at (UTC ISO), is_all_day, location, meet_link, attendees,
    organizer_email}}. Convert start_at to IST when narrating. For
    each event tomorrow in the 13:00–18:00 work window, name it with
    its time (e.g. "14:30 — Investor call with Ankur (Meet link)").
    If there's a training-block conflict (event starts before 13:00
    or after 18:00 IST), flag it: "14 conflicts: 09:00 event overlaps
    with your movement block."
  - `today_events` already happened — use sparingly, mainly to
    acknowledge completed meetings in the "what we did today" section.

CRITICAL — reading the "Kunal" training log in Apple Notes:
  The numbers in the training log (Stretch 311, Meditate 317, Breathe 205,
  Movement 203, Skill 178, Workout 178) are MISSED sessions, not completed
  ones. They represent a DEBT COUNTER that Saturday and Sunday catch-up
  days are meant to retire. A decreasing count = recovery on schedule;
  a rising count = the compass is slipping. NEVER describe these as
  "sessions done" or as volume achieved.

If `notes_signal.missed_sessions.wow_delta` is present, report week-over-week
movement per type in plain language — e.g. "Stretch down 7, Meditate flat,
Skill up 3 — three types closing gap, one growing." If `wow_delta` is null
(baseline still being collected, typical for the first week), say so briefly:
"Baseline still collecting — N of 7 days in. Real trend next week."
The `direction` dict maps each type to "gap closed" / "gap grew" / "flat".
When you have it, use those words.

TONIGHT'S CATCH-UP — `notes_signal.catchup_tonight`:
  - status="applied": Kunal replied to the 21:30 prompt and the Kunal-note
    counters were decremented. Narrate it plainly — e.g. "Catch-up tonight:
    +2h meditate, +1h workout. Meditate debt 317 → 315, workout 178 → 177."
    Use the `applied` dict for per-type sessions credited and the `before`
    / `after` dicts for the exact numbers.
  - status="no_reply": Kunal didn't reply by 22:00. Say so without
    scolding — "No catch-up reply tonight; Saturday's window still open."
  - status="parsed_empty": he replied but with no hours logged. Treat
    the same as no_reply.
  - status="error" / "not_run": skip this section silently.

EMAIL (`email_summary`):
  - `digest_24h` contains today's inbound snapshot AFTER filtering out
    noreply/newsletter/bank-alert noise. `real_inbound` is the count
    that actually matters; `unread` and `action_needed` are subsets.
    `notable` is up to 10 items with `from`, `subject`, `snippet`.
  - `unanswered_14d_top8` is messages from real humans with no reply
    from Kunal since. Each has `age_hours` + `action_needed`. If the
    list is non-empty, name the top 2-3 in tomorrow's plan — e.g.
    "Owed tomorrow: reply to Ankur (72h since pre-seed question),
    reply to Chinmay (36h since MCP review)."
  - If both are empty, inbox is clean — say that explicitly, don't
    pad.

Don't invent activity. If the signal is thin ("no meaningful activity logged today"), say so honestly and still propose tomorrow's focus from the compass and open high-priority tasks.

Rules:
- No filler. No "I hope this helps."
- No exclamation marks.
- No emoji.
- Use specific numbers, names, and amounts when you have them.
- Keep the whole brief under ~400 words.

<compass>
{compass_text[:8000] if compass_text else "(compass not loaded)"}
</compass>

<today_signal>
{signal}
</today_signal>

Write the briefing now."""

    try:
        response = await client.messages.create(
            model=astra_settings.model_sonnet,
            max_tokens=1600,
            messages=[{"role": "user", "content": prompt}],
        )
        brief_md = "\n\n".join(
            block.text for block in response.content if hasattr(block, "text")
        ).strip()
    except Exception as e:
        logger.exception("[scheduler] evening_briefing synthesis failed: %s", e)
        brief_md = (
            f"Evening briefing — {now_ist.strftime('%A %d %b %Y')}\n\n"
            "Synthesis failed. Raw signal:\n"
            f"- Tasks done: {len(tasks_done)}\n"
            f"- Agent turns: {usage_today['turns']} · ${usage_today['cost_usd']:.2f}\n"
            f"- Memories written: {len(memories_today)}"
        )

    # ── persist as memory ──────────────────────────────────
    async with async_session() as session:
        await store_memory(
            session=session,
            content=brief_md,
            memory_type=MemoryType.EPISODIC,
            source="scheduler",
            tags="briefing,evening,proactive,daily",
            importance=0.55,
        )

    # ── primary: macOS notification ────────────────────────
    # The brief lives on /briefing in the web app; the notification
    # pulls Kunal there (URL on clipboard for cmd+V).
    channel = (astra_settings.briefing_channel or "notification").lower()
    briefing_url = (
        astra_settings.astra_web_base_url.rstrip("/") + "/briefing"
    )
    notify_ok = False
    try:
        from astra.notifications import notify as _notify

        # One-liner preview from the first non-empty line of the brief
        # (usually the date header). Kept tight so macOS doesn't clip.
        first_line = next(
            (ln.strip() for ln in brief_md.splitlines() if ln.strip()),
            "briefing ready",
        )
        notify_ok = _notify(
            title="astra · evening brief",
            subtitle=now_ist.strftime("%a %d %b"),
            body=first_line[:180],
            url=briefing_url,
        )
    except Exception as e:
        logger.warning("[scheduler] evening notify failed: %s", e)

    # ── secondary: email (opt-in via channel) ──────────────
    send_result = None
    if channel in ("email", "both"):
        try:
            subject = f"astra · evening brief · {now_ist.strftime('%a %d %b')}"
            from astra.email.client import BASE_URL as _email_base, mesh_headers

            async with httpx.AsyncClient(timeout=15) as client2:
                r = await client2.post(
                    f"{_email_base}/api/v1/messages/send",
                    headers=mesh_headers(),
                    json={
                        "to": ["kunalsingh0036@gmail.com"],
                        "cc": [],
                        "bcc": [],
                        "subject": subject,
                        "body": brief_md,
                    },
                )
                if r.status_code == 200:
                    send_result = r.json()
                    logger.info("[scheduler] evening_briefing sent: %s", send_result)
                else:
                    logger.warning(
                        "[scheduler] evening_briefing email failed: %s %s",
                        r.status_code,
                        r.text[:200],
                    )
        except Exception as e:
            logger.exception("[scheduler] evening_briefing email error: %s", e)

    return {
        "status": "success",
        "channel": channel,
        "notify_ok": notify_ok,
        "brief_preview": brief_md[:300],
        "sent": send_result,
        "signal_summary": {
            "tasks_done": len(tasks_done),
            "agent_turns": usage_today["turns"],
            "memories_written": len(memories_today),
        },
    }


# ---------------------------------------------------------------------------
# Safe wrappers — what the scheduler actually calls.
# ---------------------------------------------------------------------------

async def _safe(name: str, fn):
    """Run a job, catching + logging all exceptions so one bad job never
    takes down the scheduler loop."""
    try:
        return await fn()
    except Exception as e:
        logger.exception("[scheduler] job %s crashed: %s", name, e)
        return {"status": "error", "error": str(e)}


async def run_morning_briefing():
    # v2 (briefing_v2.py): decision document — calendar + triaged
    # inbox + honest fleet + training + research, synthesized by
    # Claude against the compass, delivered memory→push→WhatsApp.
    # The v1 impls above are retained for reference/fallback but no
    # longer scheduled.
    from astra.scheduler.briefing_v2 import morning_briefing_v2

    return await _safe("morning_briefing", morning_briefing_v2)


async def run_evening_briefing():
    from astra.scheduler.briefing_v2 import evening_briefing_v2

    return await _safe("evening_briefing", evening_briefing_v2)


async def _nudge_drafts_ready(n: int) -> None:
    """Deliver staged reply drafts INTO WhatsApp — content, not a link.

    The Jul-02 audit verdict: 0 of 18 drafts ever sent, because the
    nudge pointed at a web queue Kunal never visits. The consumption
    fix: put the draft text itself in the WhatsApp message so he can
    act by replying ("send the X one" / "edit ...: shorter" / "skip").
    Nothing sends without his explicit say-so — the gate is unchanged,
    only the review surface moved to where he already lives.
    Best-effort: a failed nudge must never fail the triage job."""
    import os

    import httpx

    def _name(addr: str) -> str:
        import re as _re

        m = _re.match(r'\s*"?([^"<]+?)"?\s*<', (addr or "").strip())
        if m and m.group(1).strip():
            return m.group(1).strip()[:30]
        a = (addr or "").strip().strip("<> ")
        return (a.split("@")[0] if "@" in a else a)[:30]

    # Pull the actual pending drafts so the message carries content.
    drafts: list[dict] = []
    try:
        base = os.environ.get(
            "EMAIL_AGENT_URL", "http://email.railway.internal:8080"
        ).rstrip("/")
        headers = {
            "x-astra-secret": os.environ.get("AGENT_SHARED_SECRET", "").strip()
        }
        async with httpx.AsyncClient(timeout=20.0) as c:
            r = await c.get(
                f"{base}/api/v1/drafts/",
                params={"status": "ready", "limit": 3},
                headers=headers,
            )
        if r.status_code == 200:
            drafts = r.json() or []
    except Exception as e:
        logger.info("[scheduler] draft-content fetch skipped: %s", e)

    plural = "reply" if n == 1 else "replies"
    if drafts:
        blocks = []
        for d in drafts[:2]:
            to = _name((d.get("to_addresses") or [""])[0])
            subj = (d.get("subject") or "").strip()[:70]
            body = (d.get("body_text") or "").strip()
            if len(body) > 450:
                body = body[:450].rstrip() + "…"
            blocks.append(f"→ *To {to}* — {subj}\n{body}")
        more = len(drafts) - len(blocks)
        text = (
            f"📩 {n} {plural} drafted, waiting on you.\n\n"
            + "\n\n".join(blocks)
            + (f"\n\n(+{more} more — say “show my drafts”)" if more > 0 else "")
            + "\n\nReply to act: “send the <name> one”, "
            "“edit the <name> one: <how>”, or “skip it”. "
            "Nothing sends without you."
        )
    else:
        # Fetch failed — fall back to the old come-look tap.
        text = (
            f"📩 {n} {plural} drafted and waiting for you.\n"
            f"Say “show my drafts” to review them here, or open Astra → Replies."
        )

    # WhatsApp via the gateway (Kunal's primary surface).
    try:
        base = os.environ.get(
            "GATEWAY_URL", "http://whatsapp.railway.internal:8080"
        ).rstrip("/")
        headers = {
            "x-astra-secret": os.environ.get("AGENT_SHARED_SECRET", "").strip()
        }
        async with httpx.AsyncClient(timeout=30.0) as c:
            await c.post(
                f"{base}/api/v1/notify/owner", json={"text": text}, headers=headers
            )
    except Exception as e:
        logger.info("[scheduler] draft nudge WA skipped: %s", e)

    # Web Push to the PWA, deep-linking the Replies page.
    try:
        from astra.notifications import notify

        notify(
            title="Replies drafted",
            body=f"{n} {plural} ready to review & send",
            url="/replies",
            tag="drafts-ready",
            also_push=True,
        )
    except Exception as e:
        logger.info("[scheduler] draft nudge push skipped: %s", e)


async def inbox_triage() -> dict:
    """Silent triage before 13:00 — stage reply drafts for action-
    needed mail via the email agent (mesh HTTP). Operating-mode
    contract: by the time Kunal looks up, replies are WAITING.

    Triage stages the drafts silently; once they land we send ONE
    nudge (WhatsApp + push) so the loop actually closes — see
    _nudge_drafts_ready."""
    import os

    import httpx

    base = os.environ.get(
        "EMAIL_AGENT_URL", "http://email.railway.internal:8080"
    ).rstrip("/")
    headers = {
        "x-astra-secret": os.environ.get("AGENT_SHARED_SECRET", "").strip()
    }
    try:
        async with httpx.AsyncClient(timeout=300.0) as c:
            r = await c.post(f"{base}/api/v1/ai/triage", headers=headers)
            if r.status_code != 200:
                logger.warning(
                    "[scheduler] inbox_triage → %s: %s",
                    r.status_code,
                    r.text[:200],
                )
                return {"ok": False, "status": r.status_code}
            result = r.json()
            logger.info("[scheduler] inbox_triage: %s", result)
            drafted = int(result.get("drafted") or 0)
            if drafted > 0:
                await _nudge_drafts_ready(drafted)
            return result
    except Exception as e:
        logger.warning("[scheduler] inbox_triage error: %s", e)
        return {"ok": False, "error": str(e)}


async def run_inbox_triage():
    return await _safe("inbox_triage", inbox_triage)


async def voice_learning() -> dict:
    """Weekly: distill Kunal's voice from how he edited drafts before
    sending (the feedback loop), so the drafter compounds toward how he
    actually writes. No-op until there are enough edited samples."""
    import os

    import httpx

    base = os.environ.get(
        "EMAIL_AGENT_URL", "http://email.railway.internal:8080"
    ).rstrip("/")
    headers = {"x-astra-secret": os.environ.get("AGENT_SHARED_SECRET", "").strip()}
    try:
        async with httpx.AsyncClient(timeout=120.0) as c:
            r = await c.post(f"{base}/api/v1/ai/learn-voice", headers=headers)
            if r.status_code != 200:
                logger.warning(
                    "[scheduler] voice_learning → %s: %s", r.status_code, r.text[:200]
                )
                return {"ok": False, "status": r.status_code}
            result = r.json()
            logger.info("[scheduler] voice_learning: %s", result)
    except Exception as e:
        logger.warning("[scheduler] voice_learning error: %s", e)
        return {"ok": False, "error": str(e)}

    # Weekly re-mine of the sent-mail voice registers (best-effort; the
    # corpus grows as he sends, so the profiles keep converging on him).
    try:
        async with httpx.AsyncClient(timeout=300.0) as c:
            r = await c.post(f"{base}/api/v1/voice/mine", headers=headers)
            logger.info("[scheduler] voice_mine: %s", r.text[:300])
    except Exception as e:
        logger.warning("[scheduler] voice_mine error: %s", e)

    return result


async def run_voice_learning():
    return await _safe("voice_learning", voice_learning)


async def run_content_draft():
    """07:30 IST — draft a LinkedIn post from today's research briefing
    and stage it for review (beachhead 2)."""
    from astra.scheduler.content import content_draft

    return await _safe("content_draft", content_draft)


async def run_notes_sync():
    return await _safe("notes_sync", notes_sync)


async def run_missed_session_snapshot():
    return await _safe("missed_session_snapshot", missed_session_snapshot)


async def run_fleet_health_check():
    return await _safe("fleet_health_check", fleet_health_check)


async def run_memory_consolidation():
    return await _safe("memory_consolidation", memory_consolidation)


async def run_gmail_watch_renew():
    return await _safe("gmail_watch_renew", gmail_watch_renew)


async def run_cost_report():
    return await _safe("cost_report", cost_report)


async def run_training_catchup_prompt():
    """21:30 IST — send Kunal the catch-up check-in email."""
    from astra.scheduler.catchup import training_catchup_prompt as _tcp

    return await _safe("training_catchup_prompt", _tcp)


async def run_apply_approved_catchups():
    """Every 60s — pick up any 'approved' catchup_approvals rows and
    write them to the Kunal Apple Note."""
    from astra.scheduler.catchup import apply_approved_catchups as _apply

    return await _safe("apply_approved_catchups", _apply)


async def calendar_sync() -> dict:
    """Pull the 14-day Google Calendar window into calendar_events."""
    from astra.calendar.harvester import sync_all

    report = await sync_all()
    logger.info(
        "[scheduler] calendar_sync: %d seen, %d upserted, %d cancelled in %dms",
        report.get("total_seen", 0),
        report.get("upserted", 0),
        report.get("cancelled", 0),
        report.get("elapsed_ms", 0),
    )
    return report


async def run_calendar_sync():
    return await _safe("calendar_sync", calendar_sync)


async def run_apply_approved_events():
    """Every 60s — pick up approved calendar_event_proposals rows and
    POST them to Google Calendar via the events API."""
    from astra.calendar.writeback import apply_approved_proposals

    return await _safe("apply_approved_events", apply_approved_proposals)


async def run_meetings_pipeline():
    """Every 30s — scan ~/Astra/recordings for new audio files and
    advance any pending meeting rows (detect → transcribe → summarize)."""
    from astra.meetings.pipeline import scan_and_process

    return await _safe("meetings_pipeline", scan_and_process)


async def run_meeting_capture_trigger():
    """Every 60s — schedule/start/stop calendar-triggered captures."""
    from astra.meetings.calendar_trigger import tick

    return await _safe("meeting_capture_trigger", tick)


async def run_daily_research():
    """07:00 IST daily — Research Intel runs today's rotating topic.

    Saturday routes through meta_review instead (deeper self-audit).
    Sunday uses the standard runner but on the Sunday open-research slot.
    """
    from datetime import datetime, timedelta, timezone
    ist = timezone(timedelta(hours=5, minutes=30))
    weekday = datetime.now(ist).weekday()

    if weekday == 5:  # Saturday
        from astra.research.meta_review import run_meta_review
        return await _safe("research_meta_review", run_meta_review)

    from astra.research.runner import run_scheduled_daily
    return await _safe("research_scheduled_daily", run_scheduled_daily)


async def inbox_preview() -> dict:
    """12:45 IST — lands 15 min before the 13:00 work window.

    Emits a macOS notification pointing at /email with a one-line
    summary of what's actually worth opening. Stores the digest +
    unanswered list as an episodic memory so the evening briefing
    (and Astra Core via MCP) can reference it.
    """
    from astra.email.signals import daily_digest, unanswered_incoming
    from astra.memory.models import MemoryType
    from astra.memory.store import store_memory
    from astra.db.engine import async_session
    from astra.notifications import notify
    from astra.config import settings as astra_settings

    digest = await daily_digest(window_hours=24)
    unanswered = await unanswered_incoming(days=14)

    # Compose the memory body — dense but readable.
    lines = [
        f"Inbox preview · last 24h · {digest.get('real_inbound', 0)} real inbound",
        f"  unread: {digest.get('unread', 0)} · action_needed: {digest.get('action_needed', 0)}",
        f"  filtered noise: {digest.get('noise_count', 0)}",
    ]
    if digest.get("notable"):
        lines.append("")
        lines.append("Notable today:")
        for m in digest["notable"][:6]:
            flag = "!" if m["action_needed"] else "•"
            lines.append(f"  {flag} {m['from'][:40]} — {m['subject'][:80]}")

    if unanswered:
        lines.append("")
        lines.append(f"Unanswered humans (top {min(5, len(unanswered))}):")
        for m in unanswered[:5]:
            lines.append(
                f"  {m['age_hours']:>5.1f}h  {m['from'][:40]}  —  {m['subject'][:70]}"
            )

    body_md = "\n".join(lines)
    logger.info("[scheduler] inbox_preview: %s", body_md.replace("\n", " | ")[:300])

    async with async_session() as s:
        await store_memory(
            session=s,
            content=body_md,
            memory_type=MemoryType.EPISODIC,
            source="scheduler",
            tags="email,preview,proactive,daily",
            importance=0.45,
        )

    # Notification headline — what most deserves Kunal's first click.
    if unanswered:
        top = unanswered[0]
        head = f"Owed: {top['from'][:36]} · {top['age_hours']:.0f}h"
    elif digest.get("action_needed", 0):
        head = f"{digest['action_needed']} action-needed · open /email"
    elif digest.get("unread", 0):
        head = f"{digest['unread']} unread (no action flagged)"
    else:
        head = "inbox clean"

    base = astra_settings.astra_web_base_url.rstrip("/")
    notify(
        title="astra · inbox",
        subtitle="13:00 work window",
        body=head[:180],
        url=f"{base}/email",
    )

    return {
        "status": "success",
        "real_inbound_24h": digest.get("real_inbound", 0),
        "unanswered_count": len(unanswered),
        "notification_headline": head,
    }


async def run_inbox_preview():
    return await _safe("inbox_preview", inbox_preview)


async def classify_sweep_job() -> dict:
    """Classify up to 80 unclassified inbound messages per tick.

    Runs at 12:40 IST (5 min before the 12:45 inbox_preview), so the
    notification lands on a freshly-categorized inbox. Also runs a
    lighter sweep every 30 min as a background backstop for whatever
    Gmail push delivered during the day.
    """
    from astra.email.classify import classify_sweep

    return await classify_sweep(max_messages=80, include_retries=True)


async def run_classify_sweep():
    return await _safe("classify_sweep", classify_sweep_job)


async def classify_sweep_light() -> dict:
    """Smaller tick — 15 messages, retries disabled so we don't
    re-burn tokens on the same stuck rows every half hour."""
    from astra.email.classify import classify_sweep

    return await classify_sweep(max_messages=15, include_retries=False)


async def run_classify_sweep_light():
    return await _safe("classify_sweep_light", classify_sweep_light)


async def shares_pipeline() -> dict:
    """Every 30s — walk shares in state='received' and route each to
    memory/task/meeting via Claude Haiku classification."""
    from astra.shares.pipeline import tick

    return await tick()


async def run_shares_pipeline():
    return await _safe("shares_pipeline", shares_pipeline)


# ── Retention + ingestion (added 2026-06-11, fix-order #9/#10) ──


async def retention_sweep() -> dict:
    """Prune unbounded tables to their approved retention windows.

    Windows approved by Kunal 2026-06-11:
      - turn_events: 30 days (replay/resume only needs recent turns;
        the turns table keeps the conversation itself)
      - bridge_calls: 14 days (includes rows stuck at 'running' from
        the old zero-margin timeout bug)
      - previews: TTL already on each row (default 7d) — this finally
        CALLS sweep_expired(), which existed since the previews table
        landed but had zero call sites while multi-MB base64 uploads
        accumulated.
      - turns.messages: kept forever, deliberately — it's the
        conversation history.
    """
    from astra.db.engine import async_session
    from astra.runtime.preview_store import sweep_expired
    from sqlalchemy import text as _text

    counts: dict[str, int] = {}
    async with async_session() as session:
        r = await session.execute(
            _text(
                "DELETE FROM turn_events "
                "WHERE created_at < now() - interval '30 days'"
            )
        )
        counts["turn_events"] = r.rowcount or 0
        r = await session.execute(
            _text(
                "DELETE FROM bridge_calls "
                "WHERE created_at < now() - interval '14 days'"
            )
        )
        counts["bridge_calls"] = r.rowcount or 0
        await session.commit()

    counts["previews"] = await sweep_expired()

    logger.info("[scheduler] retention_sweep: %s", counts)
    return counts


async def run_retention_sweep():
    return await _safe("retention_sweep", retention_sweep)


async def email_sync() -> dict:
    """Trigger a Gmail sync cycle on the email agent (HTTP, mesh auth).

    Cloud replacement for the celery-beat ingestion path that was
    never deployed — the reason prod's message store sat at 0
    messages while /health said healthy.
    """
    from astra.email.client import trigger_sync

    result = await trigger_sync()
    if result.get("ok"):
        logger.info(
            "[scheduler] email_sync: %d account(s), %d new message(s)%s",
            result.get("accounts_synced", 0),
            result.get("messages_synced", 0),
            " (bootstrapped)" if result.get("bootstrapped") else "",
        )
    else:
        logger.warning("[scheduler] email_sync failed: %s", result)
    return result


async def run_email_sync():
    return await _safe("email_sync", email_sync)


async def gmail_auth_check() -> dict:
    """Probe the email agent's REAL Gmail-auth liveness and alarm LOUD if
    it's dead. The Jun-2026 blackout (refresh token expired → 8 days
    dark, no read OR send) was reported to Kunal as 'inbox quiet'. This
    makes a dead inbox scream instead — WhatsApp + push, with the fix."""
    import os

    import httpx

    base = os.environ.get(
        "EMAIL_AGENT_URL", "http://email.railway.internal:8080"
    ).rstrip("/")
    ok = False
    reason = "unknown"
    try:
        async with httpx.AsyncClient(timeout=30.0) as c:
            r = await c.get(f"{base}/health/gmail")
        body = {}
        try:
            body = r.json()
        except Exception:
            pass
        ok = r.status_code == 200 and bool(body.get("ok"))
        reason = "" if ok else str(body.get("reason") or f"status {r.status_code}")
    except Exception as e:
        ok = False
        reason = f"probe failed: {e}"

    if ok:
        logger.info("[scheduler] gmail_auth_check: OK")
        return {"ok": True}

    logger.error("[scheduler] gmail_auth_check: GMAIL AUTH DEAD — %s", reason)
    msg = (
        "🔴 Gmail auth is DOWN — Astra can't read or send your email.\n"
        f"Reason: {reason}\n"
        "Fix: run  python3 scripts/gmail_reauth.py  (re-auth + redeploy), "
        "then publish the GCP OAuth app to stop the weekly expiry."
    )
    try:
        gw = os.environ.get(
            "GATEWAY_URL", "http://whatsapp.railway.internal:8080"
        ).rstrip("/")
        headers = {
            "x-astra-secret": os.environ.get("AGENT_SHARED_SECRET", "").strip()
        }
        async with httpx.AsyncClient(timeout=30.0) as c:
            await c.post(
                f"{gw}/api/v1/notify/owner", json={"text": msg}, headers=headers
            )
    except Exception as e:
        logger.info("[scheduler] gmail alarm WA skipped: %s", e)
    try:
        from astra.notifications import notify

        notify(
            title="🔴 Gmail auth DOWN",
            body="Astra can't read/send email — re-auth needed",
            url="/email",
            tag="gmail-auth-dead",
            also_push=True,
        )
    except Exception as e:
        logger.info("[scheduler] gmail alarm push skipped: %s", e)
    return {"ok": False, "reason": reason}


async def run_gmail_auth_check():
    return await _safe("gmail_auth_check", gmail_auth_check)


async def wa_dispatch() -> dict:
    """Drain the WhatsApp gateway's outbound queue (mesh-auth HTTP).

    Cloud replacement for the celery-beat process_queue task that was
    never deployed — QUEUED messages sat unsent forever. The gateway
    re-validates session windows + cooldowns per message at send time,
    so draining frequently is safe.
    """
    import os

    import httpx

    base = os.environ.get(
        "GATEWAY_URL", "http://whatsapp.railway.internal:8080"
    ).rstrip("/")
    headers = {
        "x-astra-secret": os.environ.get("AGENT_SHARED_SECRET", "").strip()
    }
    try:
        async with httpx.AsyncClient(timeout=60.0) as c:
            r = await c.post(f"{base}/api/v1/queue/drain", headers=headers)
            if r.status_code != 200:
                logger.warning(
                    "[scheduler] wa_dispatch → %s: %s",
                    r.status_code,
                    r.text[:200],
                )
                return {"ok": False, "status": r.status_code}
            result = r.json()
            if result.get("dispatched"):
                logger.info("[scheduler] wa_dispatch: %s", result)
            return {"ok": True, **result}
    except Exception as e:
        logger.warning("[scheduler] wa_dispatch error: %s", e)
        return {"ok": False, "error": str(e)}


async def run_wa_dispatch():
    return await _safe("wa_dispatch", wa_dispatch)


async def weekly_review() -> dict:
    """Sunday 21:00 IST — the compass question, answered proactively:
    'where am I losing money or attention?' Gathers all four business
    operating pictures + training, has Claude take a position (not a
    summary), delivers like the briefings."""
    from astra.scheduler.briefing_v2 import _compass_text, _deliver, _synthesize
    from astra.tools.business_state_tools import (
        apex_state_tool,
        bay_state_tool,
        helm_state_tool,
        topstudios_state_tool,
    )

    sections: dict = {"compass": _compass_text()}
    for name, t in (
        ("helmtech", helm_state_tool),
        ("apex", apex_state_tool),
        ("bay", bay_state_tool),
        ("top studios", topstudios_state_tool),
    ):
        try:
            out = await t.handler({})
            sections[name] = out["content"][0]["text"]
        except Exception as e:
            sections[name] = f"state unavailable ({e})"

    # Reuse the briefing synthesizer with a review-specific framing
    # smuggled through the data block — cheaper than a third prompt
    # path, and the fallback behavior comes free.
    sections["instruction for this review"] = (
        "This is the WEEKLY REVIEW, not a daily brief. Answer ONE "
        "question with a position: where is Kunal losing money or "
        "attention right now? Rank the four businesses by how much "
        "they need him this week vs how much they're getting. Call "
        "out the single biggest mismatch and propose the ONE "
        "reallocation that fixes it. End with one question whose "
        "answer would change next week's plan."
    )
    body = await _synthesize("evening", sections)
    delivered = await _deliver("weekly-review", body)
    logger.info("[scheduler] weekly_review delivered: %s", delivered)
    return {"status": "success", "review": body, "delivered": delivered}


async def run_weekly_review():
    return await _safe("weekly_review", weekly_review)


async def self_improve_scan() -> dict:
    """Saturday 20:00 IST — Astra examining its own week.

    Scans the operational record for failure patterns (failed turns,
    autonomy denials, error events) and files ONE consolidated
    observation into the self_improvements queue per pattern class.
    Kunal reviews via list_self_improvements / the existing propose →
    approve → apply pipeline (test-gated code edits). The queue
    existed since Layer 4; nothing ever FED it proactively — this is
    the feed.
    """
    from sqlalchemy import text as _sql

    from astra.db.engine import async_session

    findings: list[tuple[str, str]] = []  # (severity, observation)
    async with async_session() as s:
        # 1. Failed turns this week, grouped by error shape
        r = await s.execute(
            _sql(
                """
                SELECT left(coalesce(error_message, 'unknown'), 90) AS err,
                       count(*) AS n
                FROM turns
                WHERE status = 'failed'
                  AND started_at >= now() - interval '7 days'
                GROUP BY err ORDER BY n DESC LIMIT 5
                """
            )
        )
        failed = r.fetchall()
        total_failed = sum(row.n for row in failed)
        if total_failed >= 3:
            tops = "; ".join(f"{row.n}× {row.err}" for row in failed[:3])
            findings.append(
                (
                    "medium" if total_failed < 10 else "high",
                    f"{total_failed} failed turn(s) this week. Top error "
                    f"shapes: {tops}. If one shape dominates, it's a "
                    "class bug, not noise.",
                )
            )

        # 2. Autonomy denials — repeated denials of the same tool
        # mean the gate and the model are fighting; either the tier
        # is wrong or the model needs prompting away from the tool.
        r = await s.execute(
            _sql(
                """
                SELECT tool_name, count(*) AS n FROM audit_events
                WHERE decision = 'deny'
                  AND ts >= now() - interval '7 days'
                GROUP BY tool_name HAVING count(*) >= 5
                ORDER BY n DESC LIMIT 3
                """
            )
        )
        for row in r.fetchall():
            findings.append(
                (
                    "low",
                    f"Tool {row.tool_name} denied {row.n}× this week — "
                    "tier misregistered, or the model keeps reaching for "
                    "a tool it can't have. Worth a standing grant or a "
                    "prompt nudge.",
                )
            )

        # 3. Approvals nobody resolved (expired) — the ask flow is
        # generating questions Kunal isn't answering; either over-
        # asking or the surfaces aren't visible enough.
        r = await s.execute(
            _sql(
                """
                SELECT count(*) FROM approvals
                WHERE status = 'expired'
                  AND created_at >= now() - interval '7 days'
                """
            )
        )
        expired = r.scalar() or 0
        if expired >= 3:
            findings.append(
                (
                    "medium",
                    f"{expired} approval(s) expired unanswered this week — "
                    "either the gate over-asks (grant the repeat "
                    "offenders) or approval surfaces need more reach.",
                )
            )

        # File findings, skipping duplicates still open in the queue.
        filed = 0
        for severity, obs in findings:
            dup = await s.execute(
                _sql(
                    """
                    SELECT 1 FROM self_improvements
                    WHERE status IN ('observed', 'proposed')
                      AND observation = :o
                    """
                ),
                {"o": obs},
            )
            if dup.first():
                continue
            await s.execute(
                _sql(
                    """
                    INSERT INTO self_improvements
                        (source, observation, severity, status)
                    VALUES ('weekly_scan', :o, :sev, 'observed')
                    """
                ),
                {"o": obs, "sev": severity},
            )
            filed += 1
        await s.commit()

    logger.info(
        "[scheduler] self_improve_scan: %d finding(s), %d filed",
        len(findings),
        filed,
    )
    return {"findings": len(findings), "filed": filed}


async def run_self_improve_scan():
    return await _safe("self_improve_scan", self_improve_scan)
