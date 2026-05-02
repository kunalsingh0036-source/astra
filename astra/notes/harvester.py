"""
Apple Notes harvester.

Uses `osascript` to read notes from the Notes app on the local Mac.
This is the only reliable path — the sqlite backing store uses a
proprietary protobuf format for bodies that's not cleanly decodable.

Design:
  - Pull the full list once per sync (50 notes takes <1s).
  - For each note: compare modification date against DB; if newer
    or missing, fetch the body and upsert.
  - Strip base64 image data URIs before storing plain text so the
    embedded images don't bloat the LLM context.

Permission note:
  The first time Notes access is requested via osascript, macOS
  shows an Automation permission dialog. The parent process
  (Terminal, scheduler) must have "Notes" ticked in System Settings
  → Privacy & Security → Automation. If access is denied we degrade
  gracefully — the harvester logs a warning and does nothing.
"""

from __future__ import annotations

import hashlib
import logging
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


# ─── AppleScript helpers ───────────────────────────────────────────


def _osa(script: str, timeout: int = 45) -> str:
    """Run AppleScript, return stdout. Raises on any error."""
    result = subprocess.run(
        ["/usr/bin/osascript", "-e", script],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"osascript failed: {result.stderr.strip()[:400]}"
        )
    return result.stdout


_SEP = "\x1f"  # ASCII unit separator — safe delimiter across notes
_EOR = "\x1e"  # ASCII record separator — between notes


def list_note_index() -> list[dict]:
    """Return [{apple_id, title, folder, modified_at}] for every note.

    Iterates folders → notes (not `notes of application`) because
    `folder of note` is broken in this macOS Notes AppleScript
    interface — it raises -1728. The folder→notes direction works.
    """
    script = f"""
    set sep to "{_SEP}"
    set eor to "{_EOR}"
    tell application "Notes"
      set out to ""
      repeat with f in folders
        try
          set fName to name of f
        on error
          set fName to ""
        end try
        repeat with n in notes of f
          try
            set aId to id of n
          on error
            set aId to ""
          end try
          try
            set aTitle to name of n
          on error
            set aTitle to ""
          end try
          try
            set aMod to (modification date of n) as «class isot» as string
          on error
            set aMod to ""
          end try
          try
            set aCre to (creation date of n) as «class isot» as string
          on error
            set aCre to ""
          end try
          set out to out & aId & sep & aTitle & sep & fName & sep & aMod & sep & aCre & eor
        end repeat
      end repeat
      return out
    end tell
    """
    raw = _osa(script)
    items: list[dict] = []
    seen: set[str] = set()
    for row in raw.split(_EOR):
        if not row.strip():
            continue
        parts = row.split(_SEP)
        if len(parts) < 5:
            continue
        apple_id = parts[0].strip()
        # If a note is multi-homed (e.g. iCloud + On My Mac), dedupe.
        if apple_id in seen:
            continue
        seen.add(apple_id)
        items.append(
            {
                "apple_id": apple_id,
                "title": parts[1].strip(),
                "folder": parts[2].strip(),
                "modified_at_native": _parse_iso_date(parts[3]),
                "created_at_native": _parse_iso_date(parts[4]),
            }
        )
    return items


def fetch_note_body(apple_id: str) -> str:
    """Fetch the HTML body of a single note by its AppleScript id.

    Returns the body as a raw string; the caller strips/cleans it.
    """
    # id strings from osascript contain forward slashes — escape quotes
    # properly by using AppleScript's own string concatenation.
    safe_id = apple_id.replace('"', '\\"')
    script = f"""
    tell application "Notes"
      try
        set n to note id "{safe_id}"
        return body of n
      on error errMsg
        return "__NOTE_ERROR__: " & errMsg
      end try
    end tell
    """
    out = _osa(script, timeout=60)
    if out.startswith("__NOTE_ERROR__:"):
        logger.warning("fetch_note_body failed for %s: %s", apple_id[:40], out.strip())
        return ""
    return out


# ─── Cleaning ──────────────────────────────────────────────────────


_DATA_URI_RE = re.compile(r'src="data:[^"]{100,}"', re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]+")
_MULTI_NL_RE = re.compile(r"\n{3,}")


def clean_html_to_text(html: str) -> tuple[str, str]:
    """(cleaned_html, plaintext). Strips base64 image blobs."""
    cleaned_html = _DATA_URI_RE.sub('src="[image]"', html)
    # Normalize block-level boundaries to newlines before tag strip.
    text = re.sub(
        r"</(p|div|li|h[1-6]|br|tr)[^>]*>", "\n", cleaned_html, flags=re.IGNORECASE
    )
    text = re.sub(r"<br[^>]*>", "\n", text, flags=re.IGNORECASE)
    text = _TAG_RE.sub("", text)
    # HTML entity decode
    import html as _html
    text = _html.unescape(text)
    text = _WS_RE.sub(" ", text)
    text = _MULTI_NL_RE.sub("\n\n", text).strip()
    return cleaned_html, text


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _parse_iso_date(s: str) -> datetime | None:
    s = s.strip()
    if not s:
        return None
    try:
        # Mac osx iso strings look like "2026-04-19T22:00:00+05:30"
        return datetime.fromisoformat(s).astimezone(timezone.utc)
    except ValueError:
        return None


# ─── Orchestration ────────────────────────────────────────────────


@dataclass
class SyncReport:
    total_notes_seen: int
    new_notes: int
    updated_notes: int
    unchanged_notes: int
    failed_notes: int
    elapsed_ms: int


async def sync_all(*, force: bool = False) -> SyncReport:
    """Sync every note from Apple Notes into the `apple_notes` table.

    Incremental by default: only fetches bodies for notes whose
    modified_at_native is newer than what we have on file, or that
    don't exist yet. Pass force=True to re-fetch every body.
    """
    import time
    from astra.db.engine import async_session
    from astra.notes.store import get_note_by_apple_id, upsert_note

    started = time.monotonic()
    new_count = 0
    updated_count = 0
    unchanged_count = 0
    failed_count = 0

    try:
        index = list_note_index()
    except Exception as e:
        logger.exception("[notes] list_note_index failed: %s", e)
        return SyncReport(0, 0, 0, 0, 0, int((time.monotonic() - started) * 1000))

    async with async_session() as session:
        for entry in index:
            apple_id = entry["apple_id"]
            if not apple_id:
                failed_count += 1
                continue
            existing = await get_note_by_apple_id(session, apple_id)
            needs_fetch = (
                force
                or existing is None
                or (
                    entry["modified_at_native"]
                    and existing.modified_at_native
                    and entry["modified_at_native"] > existing.modified_at_native
                )
            )
            if not needs_fetch:
                unchanged_count += 1
                continue

            try:
                raw_html = fetch_note_body(apple_id)
                cleaned_html, plaintext = clean_html_to_text(raw_html)
                h = content_hash(plaintext)

                await upsert_note(
                    session,
                    apple_id=apple_id,
                    title=entry["title"],
                    folder=entry["folder"],
                    body_html=cleaned_html,
                    body_text=plaintext,
                    content_hash=h,
                    created_at_native=entry["created_at_native"],
                    modified_at_native=entry["modified_at_native"],
                    char_count=len(plaintext),
                )
                if existing is None:
                    new_count += 1
                else:
                    updated_count += 1
            except Exception as e:
                logger.exception(
                    "[notes] sync failed for %s: %s", apple_id[:40], e
                )
                failed_count += 1

        await session.commit()

    elapsed = int((time.monotonic() - started) * 1000)
    logger.info(
        "[notes] sync complete: %d seen, +%d new, ~%d updated, =%d unchanged, x%d failed in %dms",
        len(index), new_count, updated_count, unchanged_count, failed_count, elapsed,
    )
    return SyncReport(
        total_notes_seen=len(index),
        new_notes=new_count,
        updated_notes=updated_count,
        unchanged_notes=unchanged_count,
        failed_notes=failed_count,
        elapsed_ms=elapsed,
    )
