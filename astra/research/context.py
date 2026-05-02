"""
Context gathering for Research Intel — the raw material the agent reasons over.

Two big buckets:

  1. `gather_compass()` — every memory file under the user's compass
     directory. This is Kunal's north star: three ambitions, four
     businesses, training program, schedule, feedback, learnings.
     Nothing gets recommended unless it advances the compass.

  2. `gather_astra_state()` — Astra's own internals. What services
     are running, what jobs are scheduled, what's in the DB, what's
     pending approval, what got built/completed recently. This is
     what makes the agent *self-aware* — it can recommend
     subtracting work that's stalled, or adding work that's
     obviously missing, because it sees Astra's full shape.

Both functions degrade gracefully — missing file, down service,
unreachable DB → section absent from the returned dict, never an
exception that breaks the runner.
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# Compass lives in the user's Claude memory directory.
COMPASS_DIR = Path(
    "/Users/kunalsingh/.claude/projects/"
    "-Users-kunalsingh-Claude-Code/memory"
)

# Projects the research agent should surveil alongside Astra.
ASTRA_SRC = Path("/Users/kunalsingh/Claude Code/astra")
ASTRA_WEB = Path("/Users/kunalsingh/Claude Code/astra-web")

IST = timezone(timedelta(hours=5, minutes=30))


# ──────────────────────────────────────────────────────────────────
# Compass
# ──────────────────────────────────────────────────────────────────


@dataclass
class CompassBundle:
    index_md: str = ""                        # MEMORY.md
    compass_md: str = ""                      # kunal_compass.md
    business_files: dict[str, str] = field(default_factory=dict)
    project_files: dict[str, str] = field(default_factory=dict)
    feedback_files: dict[str, str] = field(default_factory=dict)
    learnings_files: dict[str, str] = field(default_factory=dict)
    other_files: dict[str, str] = field(default_factory=dict)

    def render_for_prompt(self, *, char_budget: int = 24_000) -> str:
        """Render the bundle into a tagged block for the prompt.

        Char budget is enforced per section so no single file can swamp
        the budget. The compass itself always gets priority.
        """
        parts: list[str] = []
        remaining = char_budget

        def _add(title: str, body: str, max_chars: int) -> None:
            nonlocal remaining
            if not body:
                return
            b = body[:max_chars]
            take = min(len(b), remaining)
            if take <= 0:
                return
            parts.append(f"<{title}>\n{b[:take]}\n</{title}>")
            remaining -= take

        _add("compass", self.compass_md, 8_000)
        for name, body in self.business_files.items():
            _add(f"business:{name}", body, 3_000)
        for name, body in self.project_files.items():
            _add(f"project:{name}", body, 2_500)
        for name, body in self.feedback_files.items():
            _add(f"feedback:{name}", body, 1_000)
        for name, body in self.learnings_files.items():
            _add(f"learning:{name}", body, 1_000)

        return "\n\n".join(parts)


def gather_compass() -> CompassBundle:
    """Load every compass-relevant file. Silently skip missing ones."""
    b = CompassBundle()
    if not COMPASS_DIR.exists():
        logger.warning("[research] compass dir missing: %s", COMPASS_DIR)
        return b

    def _read(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning("[research] read %s failed: %s", path, e)
            return ""

    for f in sorted(COMPASS_DIR.glob("*.md")):
        name = f.name
        body = _read(f)
        if name == "MEMORY.md":
            b.index_md = body
        elif name == "kunal_compass.md":
            b.compass_md = body
        elif name.startswith("business_"):
            b.business_files[name] = body
        elif name.startswith("project_"):
            b.project_files[name] = body
        elif name.startswith("feedback_"):
            b.feedback_files[name] = body
        elif name.startswith("learnings_"):
            b.learnings_files[name] = body
        else:
            b.other_files[name] = body
    return b


# ──────────────────────────────────────────────────────────────────
# Astra self-state
# ──────────────────────────────────────────────────────────────────


async def gather_astra_state() -> dict[str, Any]:
    """A structured snapshot of Astra's internals.

    Every sub-gather is wrapped so one failure doesn't nuke the whole
    bundle. Sections that couldn't be collected appear as
    {"error": "..."} rather than being silently absent — the agent
    needs to see gaps honestly.
    """
    state: dict[str, Any] = {
        "now_utc": datetime.now(timezone.utc).isoformat(),
        "now_ist": datetime.now(IST).isoformat(),
    }

    state["db"] = await _safe("db", _gather_db_state())
    state["scheduler"] = await _safe("scheduler", _gather_scheduler_state())
    state["services"] = _gather_services_state()
    state["pending"] = await _safe("pending", _gather_pending())
    state["recent_activity"] = await _safe(
        "recent_activity", _gather_recent_activity()
    )
    state["training"] = await _safe("training", _gather_training())
    state["calendar_today_tomorrow"] = await _safe(
        "calendar", _gather_calendar_window()
    )
    state["meetings_recent"] = await _safe(
        "meetings", _gather_meetings_recent()
    )
    state["email"] = await _safe("email", _gather_email_signals())
    state["shares_recent"] = await _safe("shares", _gather_recent_shares())
    state["codebase"] = _gather_codebase_signals()
    return state


# ── Shares: last 24h + a counts breakdown ─────────────────────────


async def _gather_recent_shares() -> dict[str, Any]:
    """What Kunal has fed into Astra from his phone in the last day.

    Each share gets a compact line: when, where-from, what-kind, the
    LLM-written summary, the action taken (memory / task / note), and
    a 600-char head of the actual content. The briefing prompt can then
    quote a TechCrunch headline, a PDF excerpt, a forwarded message —
    instead of seeing 'something was shared' as a black box.

    Two windows: 24h (the briefing's primary lens) and 7d (so the
    recommender can spot a pattern like 'three quotations shared this
    week' without the agent having to ask)."""
    from sqlalchemy import text
    from astra.db.engine import async_session
    from astra.shares import recent_shares_for_briefing

    out: dict[str, Any] = {}
    try:
        out["last_24h"] = await recent_shares_for_briefing(hours=24, limit=20)
    except Exception as e:
        out["last_24h"] = {"error": str(e)[:300]}

    try:
        async with async_session() as s:
            r = await s.execute(text(
                """
                SELECT kind, COUNT(*)
                FROM shares
                WHERE created_at >= now() - interval '7 days'
                GROUP BY kind
                ORDER BY COUNT(*) DESC
                """
            ))
            out["counts_7d_by_kind"] = {row[0]: int(row[1]) for row in r.all()}

            r = await s.execute(text(
                """
                SELECT state, COUNT(*)
                FROM shares
                WHERE created_at >= now() - interval '7 days'
                GROUP BY state
                """
            ))
            out["counts_7d_by_state"] = {row[0]: int(row[1]) for row in r.all()}
    except Exception as e:
        out["counts_error"] = str(e)[:300]

    return out


async def _gather_email_signals() -> dict[str, Any]:
    """Inbox read via the email module — digest + unanswered + top senders.

    Every sub-call is independent so a single hiccup doesn't break the
    whole bundle. Keeps per-item sizes small because this feeds into
    the research prompt.
    """
    from astra.email.signals import (
        daily_digest, top_senders_window, unanswered_incoming,
    )

    try:
        digest = await daily_digest(window_hours=48)
    except Exception as e:
        digest = {"error": str(e)[:200]}
    try:
        unanswered = (await unanswered_incoming(days=14))[:12]
    except Exception as e:
        unanswered = {"error": str(e)[:200]}
    try:
        senders = await top_senders_window(window_days=30, limit=15)
    except Exception as e:
        senders = {"error": str(e)[:200]}

    return {
        "digest_48h": digest,
        "unanswered_14d_top12": unanswered,
        "top_senders_30d": senders,
    }


async def _safe(name: str, coro):
    """Await a coroutine-or-value, returning an error dict on failure."""
    try:
        if hasattr(coro, "__await__"):
            return await coro
        return coro
    except Exception as e:
        logger.warning("[research] gather %s failed: %s", name, e)
        return {"error": str(e)[:300]}


# ── DB counts ─────────────────────────────────────────────────────


async def _gather_db_state() -> dict[str, Any]:
    from sqlalchemy import text
    from astra.db.engine import async_session

    tables = [
        "memories",
        "tasks",
        "usage_events",
        "audit_events",
        "apple_notes",
        "missed_session_snapshots",
        "catchup_approvals",
        "calendar_events",
        "calendar_event_proposals",
        "meetings",
        "capture_sessions",
        "research_briefings",
    ]
    counts: dict[str, int] = {}
    async with async_session() as s:
        for t in tables:
            try:
                r = await s.execute(text(f"SELECT COUNT(*) FROM {t}"))
                counts[t] = int(r.scalar_one())
            except Exception:
                counts[t] = -1
    return {"row_counts": counts}


# ── Scheduler jobs ────────────────────────────────────────────────


async def _gather_scheduler_state() -> dict[str, Any]:
    """Show what the scheduler WILL register if run.

    We deliberately rebuild the scheduler from code rather than call
    `list_jobs()` on the running instance, because the runner can be
    invoked from any process (MCP tool, CLI test, etc.) where the
    in-process scheduler was never started. _build_scheduler() is a
    pure function of the source, so it's a reliable snapshot of the
    job registry regardless of caller context.
    """
    from astra.scheduler.app import _build_scheduler

    try:
        sched = _build_scheduler()
        jobs = []
        for j in sched.get_jobs():
            # next_run_time only exists on a started scheduler. Since we
            # build a fresh instance in this function, we skip it.
            try:
                nxt = getattr(j, "next_run_time", None)
                nxt_iso = nxt.isoformat() if nxt else None
            except Exception:
                nxt_iso = None
            jobs.append({
                "id": j.id,
                "name": j.name,
                "trigger": str(j.trigger),
                "next_run": nxt_iso,
            })
        # We built a detached instance — don't let it lingering-run.
        try:
            if not sched.running:
                pass  # no-op; APScheduler won't fire until .start()
        except Exception:
            pass
        return {
            "registered_count": len(jobs),
            "jobs": jobs,
        }
    except Exception as e:
        return {"error": str(e)[:300]}


# ── Service fleet ─────────────────────────────────────────────────


def _gather_services_state() -> dict[str, Any]:
    """PID-based health check by reading the astra-control pid dir."""
    pid_dir = Path("/Users/kunalsingh/Claude Code/astra-control/pids")
    if not pid_dir.exists():
        return {"error": "pid directory missing"}
    alive: dict[str, dict[str, Any]] = {}
    for pf in pid_dir.glob("*.pid"):
        name = pf.stem
        try:
            pid = int(pf.read_text().strip())
            # signal 0 = existence check
            os.kill(pid, 0)
            alive[name] = {"pid": pid, "alive": True}
        except (ProcessLookupError, PermissionError):
            alive[name] = {"pid": None, "alive": False}
        except Exception as e:
            alive[name] = {"error": str(e)[:100]}
    return {"services": alive}


# ── Pending (approvals, stuck states) ─────────────────────────────


async def _gather_pending() -> dict[str, Any]:
    from sqlalchemy import text
    from astra.db.engine import async_session

    async with async_session() as s:
        r = await s.execute(text(
            "SELECT status, COUNT(*) FROM catchup_approvals GROUP BY status"
        ))
        catchup = {row[0]: row[1] for row in r.all()}
        r = await s.execute(text(
            "SELECT status, COUNT(*) FROM calendar_event_proposals GROUP BY status"
        ))
        cal_proposals = {row[0]: row[1] for row in r.all()}
        r = await s.execute(text(
            "SELECT state, COUNT(*) FROM meetings GROUP BY state"
        ))
        meetings = {row[0]: row[1] for row in r.all()}
        r = await s.execute(text(
            "SELECT priority, COUNT(*) FROM tasks WHERE status = 'open' GROUP BY priority"
        ))
        open_tasks_by_prio = {row[0]: row[1] for row in r.all()}
        r = await s.execute(text(
            "SELECT COUNT(*) FROM tasks WHERE status = 'open' "
            "AND due_at IS NOT NULL AND due_at < now()"
        ))
        overdue = int(r.scalar_one() or 0)
    return {
        "catchup_approvals_by_status": catchup,
        "calendar_proposals_by_status": cal_proposals,
        "meetings_by_state": meetings,
        "open_tasks_by_priority": open_tasks_by_prio,
        "overdue_tasks_count": overdue,
    }


# ── Recent activity (last 24-48h) ─────────────────────────────────


async def _gather_recent_activity() -> dict[str, Any]:
    from sqlalchemy import text
    from astra.db.engine import async_session

    since_24 = datetime.now(timezone.utc) - timedelta(hours=24)
    since_7d = datetime.now(timezone.utc) - timedelta(days=7)
    async with async_session() as s:
        r = await s.execute(
            text(
                "SELECT memory_type::text, COUNT(*) FROM memories "
                "WHERE created_at >= :since GROUP BY memory_type"
            ),
            {"since": since_24},
        )
        memories_24h = {row[0]: row[1] for row in r.all()}

        r = await s.execute(
            text(
                "SELECT COUNT(*) FROM tasks "
                "WHERE completed_at >= :since"
            ),
            {"since": since_24},
        )
        tasks_done_24h = int(r.scalar_one() or 0)

        r = await s.execute(
            text(
                "SELECT COUNT(*), COALESCE(SUM(cost_usd), 0) FROM usage_events "
                "WHERE ts >= :since"
            ),
            {"since": since_24},
        )
        row = r.one()
        usage_24h = {
            "turns": int(row[0] or 0),
            "cost_usd": float(row[1] or 0),
        }

        r = await s.execute(
            text(
                "SELECT COUNT(*), COALESCE(SUM(cost_usd), 0) FROM usage_events "
                "WHERE ts >= :since"
            ),
            {"since": since_7d},
        )
        row = r.one()
        usage_7d = {
            "turns": int(row[0] or 0),
            "cost_usd": float(row[1] or 0),
        }

        r = await s.execute(
            text(
                "SELECT COUNT(*) FROM meetings "
                "WHERE created_at >= :since"
            ),
            {"since": since_7d},
        )
        meetings_7d = int(r.scalar_one() or 0)

        # Shares: how much signal Kunal pushed in via the iOS extension
        # in the last day. The briefing reasons over content via
        # `shares_recent`, but the count belongs in the activity dial
        # so a quiet day vs. a 30-share research day is visible at a
        # glance.
        r = await s.execute(
            text("SELECT COUNT(*) FROM shares WHERE created_at >= :since"),
            {"since": since_24},
        )
        shares_24h = int(r.scalar_one() or 0)

    return {
        "memories_created_24h_by_type": memories_24h,
        "tasks_completed_24h": tasks_done_24h,
        "agent_usage_24h": usage_24h,
        "agent_usage_7d": usage_7d,
        "meetings_created_7d": meetings_7d,
        "shares_received_24h": shares_24h,
    }


# ── Training ──────────────────────────────────────────────────────


async def _gather_training() -> dict[str, Any]:
    try:
        from astra.notes.missed_sessions import trend

        t = await trend(days=14)
        return {
            "today": t.get("today"),
            "week_ago": t.get("week_ago"),
            "wow_delta": t.get("wow_delta"),
            "direction": t.get("direction"),
            "baseline_days": len(t.get("series", []) or []),
        }
    except Exception as e:
        return {"error": str(e)[:300]}


# ── Calendar: today + tomorrow ────────────────────────────────────


async def _gather_calendar_window() -> dict[str, Any]:
    try:
        from astra.calendar.client import is_authorized
        from astra.calendar.store import (
            list_events_today,
            list_events_tomorrow,
        )

        authed = is_authorized()
        if not authed:
            return {"authorized": False}
        today = await list_events_today()
        tomorrow = await list_events_tomorrow()

        def _trim(ev: dict) -> dict:
            return {
                "summary": ev.get("summary", ""),
                "start_at": ev.get("start_at"),
                "end_at": ev.get("end_at"),
                "has_meet": bool(ev.get("meet_link")),
            }

        return {
            "authorized": True,
            "today": [_trim(e) for e in today],
            "tomorrow": [_trim(e) for e in tomorrow],
        }
    except Exception as e:
        return {"error": str(e)[:300]}


# ── Meetings: last 5 ready ────────────────────────────────────────


async def _gather_meetings_recent() -> dict[str, Any]:
    from sqlalchemy import text
    from astra.db.engine import async_session

    async with async_session() as s:
        r = await s.execute(text(
            """
            SELECT id, title, recorded_at, duration_seconds,
                   LEFT(summary, 500), jsonb_array_length(task_ids)
            FROM meetings
            WHERE state = 'ready'
            ORDER BY COALESCE(recorded_at, created_at) DESC
            LIMIT 5
            """
        ))
        rows = []
        for row in r.all():
            rows.append({
                "id": row[0],
                "title": row[1],
                "recorded_at": row[2].isoformat() if row[2] else None,
                "duration_s": row[3],
                "summary_head": row[4],
                "task_count": row[5],
            })
    return {"recent_ready": rows}


# ── Codebase signals ──────────────────────────────────────────────


def _gather_codebase_signals() -> dict[str, Any]:
    """Git log of the two main Astra repos + size signals.

    We only need the last 14 days of commits. A research brief that
    knows the last things Kunal shipped can reason about follow-on
    work without Kunal having to re-explain.
    """
    def _git_log(path: Path, limit: int = 40) -> list[dict]:
        if not (path / ".git").exists():
            return []
        try:
            result = subprocess.run(
                ["git", "-C", str(path), "log",
                 f"-n{limit}", "--since=14.days",
                 "--pretty=format:%h|%ad|%s",
                 "--date=short"],
                capture_output=True, text=True, timeout=5,
            )
            lines = [l for l in result.stdout.splitlines() if l.strip()]
            out: list[dict] = []
            for l in lines:
                parts = l.split("|", 2)
                if len(parts) == 3:
                    out.append({"sha": parts[0], "date": parts[1], "msg": parts[2]})
            return out
        except Exception:
            return []

    astra_commits = _git_log(ASTRA_SRC)
    web_commits = _git_log(ASTRA_WEB)

    # Rough size signal — lines of Python in astra/.
    py_count = 0
    if ASTRA_SRC.exists():
        for f in ASTRA_SRC.rglob("*.py"):
            if "__pycache__" in str(f):
                continue
            try:
                py_count += sum(1 for _ in f.open())
            except Exception:
                pass

    return {
        "astra_commits_14d": astra_commits,
        "astra_web_commits_14d": web_commits,
        "astra_py_loc": py_count,
    }
