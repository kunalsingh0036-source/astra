"""
Training catch-up pipeline.

Two jobs, one round-trip:

  21:30 IST — `training_catchup_prompt()`
    Emails Kunal a short, parseable prompt asking what he got done
    today across the six counters (stretch / meditate / breathe /
    movement / skill / workout). The email is intentionally templated
    so either a plain-English reply or a filled-in template works.

  22:00 IST — `evening_briefing()` (separate module)
    Before gathering signals, calls `ingest_latest_reply()` from this
    module to:
      1. Pull the most recent reply to tonight's prompt (via email-agent).
      2. Parse it via Claude into per-type minutes done.
      3. Convert minutes → sessions (1hr default; per-type target from
         kunal_compass.md — 2h stretch/meditate, 1h each of the rest).
      4. Call `apply_catchup()` on the Kunal note to decrement the debt.
      5. Return a structured result so the briefing can say:
         "Catch-up tonight: +2h meditate, +1h workout. Stretch debt
          down 2 to 309, Workout debt down 1 to 177."

The 22:00 `missed_session_snapshot` (scheduled at 21:30 today, but
re-run inside the briefing) then picks up the freshly lowered
counters and the evening brief speaks the real numbers.

Why not wait until morning? Because the whole point of the 22:00
briefing is one full-day close. Kunal can read the catch-up in the
same brief that frames tomorrow. If he replies late, the next
briefing catches it (idempotent by Gmail message id).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


IST = timezone(timedelta(hours=5, minutes=30))

# The "sessions-per-hour" ratio per counter type. Derived from the
# daily target block in the "Kunal" note:
#     2h stretch, 2h meditate, 1h breathing, 1h movement, 1h skill, 1h workout
# Each "session" = 1 hour of that activity, so 2h stretch = 2 sessions
# credited against the debt. Keep it simple; we can refine later.
SESSIONS_PER_HOUR: dict[str, float] = {
    "stretch": 1.0,
    "meditate": 1.0,
    "breathe": 1.0,
    "movement": 1.0,
    "skill": 1.0,
    "workout": 1.0,
}

# Subject line used to find replies — if this changes, the parser
# below must change with it. Keep distinctive so we don't collide
# with other astra emails.
PROMPT_SUBJECT = "astra · catch-up · what did you get done today?"

# Email-agent endpoints. The agent has a `GET /api/v1/messages/`
# list endpoint but no server-side search, so we pull the recent
# window and filter client-side on subject + direction.
EMAIL_SEND_URL = "http://localhost:8005/api/v1/messages/send"
EMAIL_LIST_URL = "http://localhost:8005/api/v1/messages/"


# ─── 21:30 prompt ───────────────────────────────────────────────────


async def training_catchup_prompt() -> dict[str, Any]:
    """21:30 IST — prompt Kunal for today's catch-up.

    Primary channel: macOS notification + `/tonight` URL on clipboard.
    Secondary channel: Gmail, if `settings.briefing_channel` is
    "email" or "both".
    """
    from astra.config import settings as astra_settings
    from astra.notifications import notify

    now_ist = datetime.now(IST)
    channel = (astra_settings.briefing_channel or "notification").lower()
    base = astra_settings.astra_web_base_url.rstrip("/")
    tonight_url = f"{base}/tonight"

    # ── primary: macOS notification ──────────────────────
    notify_ok = notify(
        title="astra · catch-up",
        subtitle=now_ist.strftime("%A %d %b"),
        body="Log today's training. URL on clipboard.",
        url=tonight_url,
    )

    # ── secondary: email (opt-in via channel) ────────────
    email_result: dict[str, Any] | None = None
    if channel in ("email", "both"):
        import httpx

        body = _build_prompt_body(now_ist, tonight_url)
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(
                    EMAIL_SEND_URL,
                    json={
                        "to": ["kunalsingh0036@gmail.com"],
                        "cc": [],
                        "bcc": [],
                        "subject": PROMPT_SUBJECT,
                        "body": body,
                    },
                )
                if r.status_code != 200:
                    logger.warning(
                        "[catchup] prompt email failed: %s %s",
                        r.status_code, r.text[:200],
                    )
                    email_result = {"status": "error", "code": r.status_code}
                else:
                    email_result = {"status": "success", "send": r.json()}
        except Exception as e:
            logger.exception("[catchup] prompt email error: %s", e)
            email_result = {"status": "error", "error": str(e)}

    return {
        "status": "success" if notify_ok else "notify_failed",
        "channel": channel,
        "url": tonight_url,
        "notify_ok": notify_ok,
        "email": email_result,
    }


def _build_prompt_body(now_ist: datetime, tonight_url: str = "") -> str:
    """Redundancy-channel email body. Primary flow is the /tonight form."""
    date_str = now_ist.strftime("%A, %d %b")
    link_line = f"{tonight_url}\n\n" if tonight_url else ""
    return (
        f"{date_str} — catch-up check-in.\n\n"
        f"Log on Astra (preferred):\n{link_line}"
        "Or reply to this email with hours done today.\n"
        "Template (edit in-line, use 0 for none):\n\n"
        "  stretch: 0\n"
        "  meditate: 0\n"
        "  breathe: 0\n"
        "  movement: 0\n"
        "  skill: 0\n"
        "  workout: 0\n\n"
        "Or free-form — e.g. \"2 hr meditate, 45 min workout\". "
        "Counters stage as a pending approval on your Kunal note until you Apply.\n"
    )


# ─── Reply ingestion (called from evening_briefing) ─────────────────


async def fetch_latest_reply() -> dict[str, Any] | None:
    """Pull the most recent reply to tonight's catch-up prompt.

    Strategy: hit the email-agent's list endpoint with direction=received
    (limit=25, newest first), then filter client-side for:
      - subject containing PROMPT_SUBJECT (accounts for "Re: " prefix)
      - sent_at strictly after 21:30 today IST (so yesterday's reply
        isn't mistakenly re-applied)

    Idempotency is guaranteed at the writeback layer via `reply_id` —
    even if we return the same message on two runs, the second call
    is a no-op.
    """
    import httpx

    now_ist = datetime.now(IST)
    today_2130_ist = now_ist.replace(hour=21, minute=30, second=0, microsecond=0)
    # If called before 21:30 (manual test run), broaden to the last 24h
    # so a test reply sent at 09:00 is still findable.
    since = now_ist - timedelta(hours=24) if now_ist < today_2130_ist else today_2130_ist
    since_utc = since.astimezone(timezone.utc)

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            # EmailDirection enum: "inbound" | "outbound" (not "received").
            r = await client.get(
                EMAIL_LIST_URL,
                params={"direction": "inbound", "limit": 25, "offset": 0},
            )
            if r.status_code != 200:
                logger.warning("[catchup] reply list failed: %s", r.status_code)
                return None
            # Endpoint returns a bare JSON array of MessageOut.
            hits = r.json()
            if not isinstance(hits, list):
                hits = hits.get("messages") or []
    except Exception as e:
        logger.warning("[catchup] reply list error: %s", e)
        return None

    def _sent_at(msg: dict) -> datetime | None:
        v = msg.get("sent_at") or msg.get("created_at")
        if not v:
            return None
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        except Exception:
            return None

    # Subject check tolerates reply prefixes like "Re:" / "Fwd:".
    subject_needle = PROMPT_SUBJECT.lower()

    def _matches(msg: dict) -> bool:
        if (msg.get("direction") or "").lower() != "inbound":
            return False
        subj = (msg.get("subject") or "").lower()
        if subject_needle not in subj:
            return False
        sa = _sent_at(msg)
        if sa is None:
            return False
        return sa >= since_utc

    candidates = [m for m in hits if _matches(m)]
    if not candidates:
        return None
    candidates.sort(key=lambda m: _sent_at(m) or since_utc, reverse=True)
    return candidates[0]


async def parse_reply_to_hours(reply_body: str) -> dict[str, float]:
    """Use Claude to extract per-type hours from a free-form reply.

    Returns a dict like {"stretch": 0.0, "meditate": 2.0, ...}.
    Types absent from the reply default to 0.
    """
    import anthropic

    from astra.config import settings as astra_settings

    api_key = astra_settings.anthropic_api_key or os.environ.get(
        "ANTHROPIC_API_KEY", ""
    )
    if not api_key:
        try:
            env_path = Path(__file__).resolve().parents[2] / ".env"
            if env_path.exists():
                for line in env_path.read_text().splitlines():
                    if line.startswith("ANTHROPIC_API_KEY="):
                        api_key = line.split("=", 1)[1].strip().strip('"').strip("'")
                        break
        except Exception:
            pass
    if not api_key:
        logger.warning("[catchup] no anthropic key; using keyword fallback")
        return _fallback_parse(reply_body)

    client = anthropic.AsyncAnthropic(api_key=api_key)
    prompt = (
        "Extract training hours done today from this user reply.\n"
        "Six counters only: stretch, meditate, breathe, movement, skill, workout.\n"
        "Return STRICT JSON — no prose, no code fences — shape:\n"
        '{"stretch": <hours>, "meditate": <hours>, "breathe": <hours>, '
        '"movement": <hours>, "skill": <hours>, "workout": <hours>}\n'
        "Use 0 for any counter not mentioned. Convert minutes to hours "
        "(e.g. 45 min → 0.75). If a value is unclear, use 0.\n\n"
        f"Reply:\n---\n{reply_body[:4000]}\n---\n"
    )
    try:
        response = await client.messages.create(
            model=astra_settings.model_haiku,
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "\n".join(
            block.text for block in response.content if hasattr(block, "text")
        ).strip()
        # Strip common wrappers in case Claude ignores the instruction.
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:].strip()
        parsed = json.loads(text)
    except Exception as e:
        logger.warning("[catchup] claude parse failed (%s); falling back", e)
        return _fallback_parse(reply_body)

    out: dict[str, float] = {}
    for k in ("stretch", "meditate", "breathe", "movement", "skill", "workout"):
        v = parsed.get(k, 0)
        try:
            out[k] = max(0.0, float(v))
        except Exception:
            out[k] = 0.0
    return out


def _fallback_parse(reply_body: str) -> dict[str, float]:
    """Keyword-only parser used when Claude is unreachable.

    Handles the explicit template ("stretch: 2") and the common shorthand
    ("2hr stretch", "45 min meditate"). Misses nuanced phrasing — the
    Claude path is the intended happy case.
    """
    import re

    out: dict[str, float] = {
        "stretch": 0.0, "meditate": 0.0, "breathe": 0.0,
        "movement": 0.0, "skill": 0.0, "workout": 0.0,
    }
    for t in out.keys():
        # template form: "stretch: 2" or "stretch - 2"
        m = re.search(rf"\b{t}\b\s*[:\-]\s*(\d+(?:\.\d+)?)", reply_body, re.I)
        if m:
            out[t] = float(m.group(1))
            continue
        # shorthand: "2hr meditate" / "45 min meditate"
        m = re.search(
            rf"(\d+(?:\.\d+)?)\s*(hr|hour|hours|min|minute|minutes)\s+(?:of\s+)?{t}\b",
            reply_body, re.I,
        )
        if m:
            val = float(m.group(1))
            unit = m.group(2).lower()
            if unit.startswith("min"):
                val = val / 60.0
            out[t] = val
    return out


def hours_to_session_decrements(hours: dict[str, float]) -> dict[str, int]:
    """Convert {type: hours} into {type: sessions} suitable for writeback."""
    out: dict[str, int] = {}
    for t, h in hours.items():
        ratio = SESSIONS_PER_HOUR.get(t, 1.0)
        sessions = int(round(h * ratio))
        if sessions > 0:
            out[t] = sessions
    return out


async def ingest_latest_reply() -> dict[str, Any]:
    """End-to-end: find tonight's reply, parse, decrement note.

    Returns a structured result for the briefing to narrate. Safe to
    call when there's no reply — returns {"status": "no_reply"}.
    """
    from astra.notes.writeback import apply_catchup

    reply = await fetch_latest_reply()
    if reply is None:
        return {"status": "no_reply"}

    body = reply.get("body_text") or reply.get("snippet") or ""
    reply_id = (
        reply.get("gmail_message_id")
        or reply.get("id")
        or f"rx-{reply.get('sent_at') or datetime.now(IST).isoformat()}"
    )

    hours = await parse_reply_to_hours(body)
    decrements = hours_to_session_decrements(hours)

    if not decrements:
        return {
            "status": "parsed_empty",
            "reply_id": reply_id,
            "hours": hours,
        }

    # Compute what WOULD happen without touching the note, so we can
    # stage the approval + show projections in the brief.
    preview = apply_catchup(decrements=decrements, reply_id=str(reply_id))

    if not preview.applied:
        return {
            "status": "no_change",
            "reply_id": reply_id,
            "hours": hours,
            "decrements": decrements,
            "reason": preview.reason,
        }

    # Route based on the writeback mode.
    from astra.config import settings as astra_settings

    mode = (astra_settings.notes_writeback_mode or "approval").lower()

    if mode == "off":
        return {
            "status": "off",
            "reply_id": reply_id,
            "hours": hours,
            "decrements": decrements,
            "would_apply": preview.applied,
            "before": preview.before,
            "projected_after": preview.after,
            "note_touched": False,
        }

    if mode == "auto":
        # Explicit opt-in — write now, no approval gate.
        written = apply_catchup(
            decrements=decrements,
            reply_id=str(reply_id),
            dry_run=False,
        )
        return {
            "status": "applied",
            "reply_id": reply_id,
            "hours": hours,
            "decrements": decrements,
            "applied": written.applied,
            "before": written.before,
            "after": written.after,
            "idempotent_skip": written.idempotent_skip,
            "note_touched": not written.idempotent_skip,
        }

    # Default — "approval": stage a pending row, return the approval URL.
    approval_id = await _stage_approval(
        reply_id=str(reply_id),
        decrements=decrements,
        before=preview.before,
        projected_after=preview.after,
        hours=hours,
    )

    base = astra_settings.astra_web_base_url.rstrip("/")
    approval_url = f"{base}/catchup/{approval_id}"

    return {
        "status": "pending_approval",
        "reply_id": reply_id,
        "approval_id": approval_id,
        "approval_url": approval_url,
        "hours": hours,
        "decrements": decrements,
        "would_apply": preview.applied,
        "before": preview.before,
        "projected_after": preview.after,
        "note_touched": False,
    }


async def _stage_approval(
    *,
    reply_id: str,
    decrements: dict[str, int],
    before: dict[str, int | None],
    projected_after: dict[str, int | None],
    hours: dict[str, float],
) -> int:
    """Insert (or update on conflict) a row in catchup_approvals.

    Idempotent by reply_id — a second briefing pass on the same reply
    returns the existing approval id rather than creating a duplicate.
    """
    from sqlalchemy import text
    from astra.db.engine import async_session

    async with async_session() as session:
        row = await session.execute(
            text(
                """
                INSERT INTO catchup_approvals
                  (reply_id, decrements, before_counters,
                   projected_after, hours_reported, status)
                VALUES
                  (:rid,
                   CAST(:dec AS JSONB),
                   CAST(:bef AS JSONB),
                   CAST(:aft AS JSONB),
                   CAST(:hrs AS JSONB),
                   'pending')
                ON CONFLICT (reply_id) DO UPDATE
                  SET decrements      = EXCLUDED.decrements,
                      projected_after = EXCLUDED.projected_after,
                      hours_reported  = EXCLUDED.hours_reported
                RETURNING id
                """
            ),
            {
                "rid": reply_id,
                "dec": json.dumps(decrements),
                "bef": json.dumps(before),
                "aft": json.dumps(projected_after),
                "hrs": json.dumps(hours),
            },
        )
        approval_id = int(row.scalar_one())
        await session.commit()
        return approval_id


async def apply_approved_catchups() -> dict[str, Any]:
    """Pick up any rows in state='approved', write to the Apple Note,
    mark as 'applied'. Also expire any 'pending' rows older than 24h.

    Runs on a short interval (60s) so an Approve click lands fast.
    """
    from sqlalchemy import text

    from astra.db.engine import async_session
    from astra.notes.writeback import apply_catchup as _write

    applied_ids: list[int] = []
    errored: list[dict[str, Any]] = []
    expired: int = 0

    async with async_session() as session:
        # Expire stale pending rows first.
        exp = await session.execute(
            text(
                """
                UPDATE catchup_approvals
                SET status = 'expired'
                WHERE status = 'pending'
                  AND created_at < now() - interval '24 hours'
                RETURNING id
                """
            )
        )
        expired = len(exp.all())

        # Pull rows ready to apply.
        rows = await session.execute(
            text(
                """
                SELECT id, reply_id, decrements
                FROM catchup_approvals
                WHERE status = 'approved'
                ORDER BY approved_at ASC
                LIMIT 10
                """
            )
        )
        pending = [(r[0], r[1], r[2]) for r in rows.all()]

        for row_id, reply_id, decrements in pending:
            try:
                result = _write(
                    decrements={k: int(v) for k, v in decrements.items()},
                    reply_id=reply_id,
                    dry_run=False,
                )
                if result.applied or result.idempotent_skip:
                    await session.execute(
                        text(
                            """
                            UPDATE catchup_approvals
                            SET status = 'applied',
                                applied_at = now()
                            WHERE id = :id
                            """
                        ),
                        {"id": row_id},
                    )
                    applied_ids.append(row_id)
                else:
                    await session.execute(
                        text(
                            """
                            UPDATE catchup_approvals
                            SET status = 'error',
                                error = :err
                            WHERE id = :id
                            """
                        ),
                        {"id": row_id, "err": result.reason or "no-op"},
                    )
                    errored.append({"id": row_id, "error": result.reason})
            except Exception as e:
                logger.exception("[catchup] apply failed for row %s", row_id)
                await session.execute(
                    text(
                        """
                        UPDATE catchup_approvals
                        SET status = 'error',
                            error = :err
                        WHERE id = :id
                        """
                    ),
                    {"id": row_id, "err": str(e)[:500]},
                )
                errored.append({"id": row_id, "error": str(e)})

        await session.commit()

    return {
        "applied_ids": applied_ids,
        "errored": errored,
        "expired_count": expired,
    }
