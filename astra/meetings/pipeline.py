"""
Meeting processing pipeline.

Every scheduler tick (default 30s), we:
  1. Scan ~/Astra/recordings/ for new audio files → insert as
     state='detected'.
  2. For each row in a non-terminal state, advance it:
        detected     → transcribing  → transcribed
        transcribed  → summarizing   → ready
     On any failure, state=error with the reason.
  3. On 'ready', stage action items as tasks and fire a macOS
     notification linking to /meetings/[id].

State transitions are idempotent — a restart mid-pipeline resumes
from the last persisted state.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text

from astra.db.engine import async_session

logger = logging.getLogger(__name__)


RECORDINGS_DIR = Path(os.path.expanduser("~/Astra/recordings"))
AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".aac", ".mp4", ".mov", ".flac", ".ogg", ".webm", ".aiff", ".aif"}

# Files still being written (growing) should be skipped. We require
# the mtime to be at least this old before picking up.
MIN_STABLE_AGE_SECONDS = 10


async def _detect_new_files() -> int:
    """Insert rows for any new audio files in RECORDINGS_DIR."""
    if not RECORDINGS_DIR.exists():
        return 0

    now_ts = datetime.now(timezone.utc).timestamp()
    new_count = 0

    async with async_session() as session:
        for p in sorted(RECORDINGS_DIR.iterdir()):
            if not p.is_file():
                continue
            if p.suffix.lower() not in AUDIO_EXTS:
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            # Skip actively-being-written files
            if now_ts - st.st_mtime < MIN_STABLE_AGE_SECONDS:
                continue

            abspath = str(p.resolve())
            existing = await session.execute(
                text("SELECT 1 FROM meetings WHERE source_path = :p"),
                {"p": abspath},
            )
            if existing.first() is not None:
                continue

            mtime_utc = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
            title = p.stem  # filename without extension

            await session.execute(
                text(
                    """
                    INSERT INTO meetings
                      (source_path, title, recorded_at, state)
                    VALUES
                      (:p, :t, :r, 'detected')
                    """
                ),
                {"p": abspath, "t": title[:511], "r": mtime_utc},
            )
            new_count += 1

        if new_count:
            await session.commit()

    if new_count:
        logger.info("[meetings] detected %d new recording(s)", new_count)
    return new_count


async def _advance_one(row: dict[str, Any]) -> None:
    """Move a single meeting row one step forward."""
    from astra.meetings.summarizer import summarize_transcript
    from astra.meetings.transcriber import transcribe_file

    row_id = row["id"]
    state = row["state"]

    try:
        if state == "detected":
            await _update(row_id, state="transcribing")
            # Run the blocking whisper call in a thread.
            result = await asyncio.to_thread(transcribe_file, row["source_path"])
            await _update(
                row_id,
                state="transcribed",
                transcript=result.text,
                duration_seconds=result.duration_s,
                model_used=result.model_used,
            )
            return

        if state == "transcribed":
            await _update(row_id, state="summarizing")
            summary = await summarize_transcript(
                row["transcript"], title=row["title"]
            )
            action_json = json.dumps(summary.action_items or [])
            await _update(
                row_id,
                state="ready",
                summary=_compose_summary_md(summary),
                action_items=action_json,
            )
            # Stage tasks + notify once the row lands in 'ready'
            await _stage_tasks(row_id, summary.action_items or [])
            await _fire_notification(row_id, row.get("title", ""), summary)
            return

        # terminal states ('ready','error'): nothing to do
    except Exception as e:
        logger.exception("[meetings] row %s failed at state %s", row_id, state)
        await _update(row_id, state="error", error=str(e)[:1000])


def _compose_summary_md(s: "MeetingSummary") -> str:  # type: ignore[name-defined]
    """Render the structured summary as a single markdown blob for storage."""
    lines: list[str] = []
    if s.gist:
        lines.append(f"**Gist.** {s.gist}")
    if s.decisions:
        lines.append("\n**Decisions.**")
        for d in s.decisions:
            lines.append(f"- {d}")
    if s.action_items:
        lines.append("\n**Action items.**")
        for a in s.action_items:
            bits = [a.get("title", "")]
            if a.get("owner") and a["owner"] not in ("unknown", ""):
                bits.append(f"_({a['owner']})_")
            if a.get("due"):
                bits.append(f"· due {a['due']}")
            lines.append("- " + " ".join(bits))
    if s.open_questions:
        lines.append("\n**Open questions.**")
        for q in s.open_questions:
            lines.append(f"- {q}")
    if s.followup_draft:
        lines.append("\n**Follow-up draft.**\n")
        lines.append(s.followup_draft)
    return "\n".join(lines)


async def _stage_tasks(meeting_id: int, items: list[dict]) -> None:
    """Insert each action_item as a row in `tasks`, link IDs back on
    the meeting so the UI can show them together."""
    if not items:
        return

    ids: list[int] = []
    async with async_session() as session:
        for a in items:
            title = (a.get("title") or "").strip()
            if not title:
                continue
            prio = int(a.get("priority") or 2)
            note = f"From meeting #{meeting_id}"
            owner = a.get("owner") or ""
            if owner and owner not in ("unknown", ""):
                note += f" · owner: {owner}"
            due = a.get("due") or ""
            r = await session.execute(
                text(
                    """
                    INSERT INTO tasks
                      (title, note, status, priority, tags, source)
                    VALUES
                      (:t, :n, 'open', :p, :tg, :src)
                    RETURNING id
                    """
                ),
                {
                    "t": title[:511],
                    "n": note,
                    "p": prio,
                    "tg": "meeting",
                    "src": f"meeting:{meeting_id}",
                },
            )
            ids.append(int(r.scalar_one()))

        if ids:
            await session.execute(
                text(
                    """
                    UPDATE meetings
                    SET task_ids = CAST(:ids AS JSONB), updated_at = now()
                    WHERE id = :mid
                    """
                ),
                {"mid": meeting_id, "ids": json.dumps(ids)},
            )
            await session.commit()

    if ids:
        logger.info("[meetings] staged %d tasks for meeting %s", len(ids), meeting_id)


async def _fire_notification(
    meeting_id: int,
    title: str,
    summary: "MeetingSummary",  # type: ignore[name-defined]
) -> None:
    """Tell Kunal the meeting summary is ready (notification + clipboard URL)."""
    try:
        from astra.config import settings
        from astra.notifications import notify

        base = settings.astra_web_base_url.rstrip("/")
        url = f"{base}/meetings/{meeting_id}"
        body = summary.gist[:160] if summary.gist else "Summary ready."
        notify(
            title="astra · meeting summary",
            subtitle=(title or f"#{meeting_id}")[:40],
            body=body,
            url=url,
        )
    except Exception as e:
        logger.warning("[meetings] notification failed: %s", e)


async def _update(row_id: int, **cols: Any) -> None:
    """Partial-update a meeting row; touches updated_at."""
    if not cols:
        return
    set_parts: list[str] = ["updated_at = now()"]
    params: dict[str, Any] = {"id": row_id}
    for k, v in cols.items():
        set_parts.append(f"{k} = :{k}")
        params[k] = v
    async with async_session() as session:
        await session.execute(
            text(f"UPDATE meetings SET {', '.join(set_parts)} WHERE id = :id"),
            params,
        )
        await session.commit()


async def scan_and_process() -> dict[str, Any]:
    """Watcher + advancer — called by the scheduler every 30 s.

    Returns a short report for logging.
    """
    detected = await _detect_new_files()

    # Pull all rows in non-terminal states.
    async with async_session() as session:
        r = await session.execute(
            text(
                """
                SELECT id, source_path, state, title, transcript
                FROM meetings
                WHERE state IN ('detected', 'transcribed')
                ORDER BY id ASC
                LIMIT 10
                """
            )
        )
        pending = [
            {
                "id": row[0],
                "source_path": row[1],
                "state": row[2],
                "title": row[3],
                "transcript": row[4],
            }
            for row in r.all()
        ]

    processed: list[int] = []
    for row in pending:
        await _advance_one(row)
        processed.append(row["id"])

    return {
        "detected_new": detected,
        "advanced": processed,
    }
