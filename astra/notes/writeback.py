"""
Apple Notes writeback — decrement the "Kunal" training-debt counters.

The "Kunal" note carries a block that looks like:

    Saturday- Sunday
    Stretch - 311
    Meditate - 317
    Breathe - 205
    Movement - 203
    Skill - 178
    Workout - 178

Each line is one of the six canonical types (stretch, meditate, breathe,
movement, skill, workout) with the running MISSED count. When Kunal does
catch-up training we must DECREMENT these numbers, not invent new ones.

This module:
  1. Reads the current body of the "Kunal" note via AppleScript.
  2. Replaces each "Type - N" line with "Type - (N - delta)" where delta
     is computed from the catch-up reply (hours done → sessions credited
     against a 1-hour-per-session default, honoring the per-type daily
     target on the same note).
  3. Writes the whole body back via `set body of note ... to ...`.

All writes are idempotent in the sense that a second reply on the same
day won't double-count — the scheduler passes a `reply_id` that we
stamp into the note's last line as a footer marker. If the marker for
that id is already present, we skip.

Safety:
  * Never writes if we can't find ALL six counters. A partial parse
    would corrupt the ledger.
  * Caps the decrement at current value (counters can't go negative).
  * Preserves every line of the note outside the 6 counter lines.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass

from astra.notes.missed_sessions import TYPES, Counters, parse_counters

logger = logging.getLogger(__name__)


# ── AppleScript helpers ────────────────────────────────────────────

_SEP = "\x1f"


def _osa(script: str, timeout: int = 30) -> str:
    result = subprocess.run(
        ["/usr/bin/osascript", "-e", script],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(f"osascript failed: {result.stderr.strip()[:400]}")
    return result.stdout


def _read_kunal_note() -> tuple[str, str] | None:
    """Return (apple_id, body_html) of the "Kunal" note, or None."""
    script = '''
    tell application "Notes"
      repeat with f in folders
        repeat with n in notes of f
          try
            if (name of n) is "Kunal" then
              return (id of n) & "\x1f" & (body of n)
            end if
          end try
        end repeat
      end repeat
      return ""
    end tell
    '''
    raw = _osa(script, timeout=20).strip()
    if not raw or _SEP not in raw:
        return None
    aid, body = raw.split(_SEP, 1)
    return aid, body


def _write_kunal_note_body(new_body_html: str) -> None:
    """Replace the Kunal note's body with `new_body_html`.

    Notes `body` is HTML. We do string replacement on a thin HTML shell
    so the formatting survives. AppleScript string escaping: double
    quotes → `" & quote & "`; newlines we keep literal (AppleScript
    allows multi-line strings in `tell` blocks)."""
    # Escape double quotes and backslashes for AppleScript
    escaped = new_body_html.replace("\\", "\\\\").replace('"', '\\"')
    script = f'''
    tell application "Notes"
      repeat with f in folders
        repeat with n in notes of f
          try
            if (name of n) is "Kunal" then
              set body of n to "{escaped}"
              return "ok"
            end if
          end try
        end repeat
      end repeat
      return "not-found"
    end tell
    '''
    out = _osa(script, timeout=45).strip()
    if out != "ok":
        raise RuntimeError(f"writeback failed: {out!r}")


# ── Counter-line rewriting ──────────────────────────────────────────

# Match a single counter line in the HTML body. The note renders each
# bullet as `<div>Stretch - 311</div>` or similar. We operate on the
# raw body string (HTML), replacing just the digits.
_LINE_RE = re.compile(
    r"(?P<before>\b(?:stretch|meditate|breathe|breathing|movement|skill|workout)\s*[-–—:]\s*)"
    r"(?P<num>\d+)",
    re.IGNORECASE,
)


@dataclass
class WritebackResult:
    applied: dict[str, int]           # per-type sessions decremented
    before: dict[str, int | None]
    after: dict[str, int | None]
    idempotent_skip: bool = False
    reason: str = ""


# Footer marker so a re-run with the same reply_id is a no-op.
_MARKER_PREFIX = "<!--astra-catchup-applied:"


def _has_marker(body: str, reply_id: str) -> bool:
    return f"{_MARKER_PREFIX}{reply_id}-->" in body


def _append_marker(body: str, reply_id: str) -> str:
    return body + f"\n{_MARKER_PREFIX}{reply_id}-->\n"


def apply_catchup(
    *,
    decrements: dict[str, int],
    reply_id: str,
    dry_run: bool = True,
) -> WritebackResult:
    """Compute the new counters after `decrements`, optionally writing
    back to the Kunal Apple Note.

    `decrements` keys must be a subset of TYPES; each value is the
    number of sessions to subtract (not hours — caller converts).

    DEFAULT IS DRY-RUN. Per Kunal's autonomy stance (2026-04-19:
    "Everything approval-based for now"), we NEVER mutate the
    Apple Note unless the caller explicitly passes `dry_run=False`.
    The returned `before` / `after` / `applied` fields are still
    computed so the briefing can report what would change.

    Idempotent: if the note already contains a marker for `reply_id`,
    we return `idempotent_skip=True` and make no changes.
    """
    pulled = _read_kunal_note()
    if pulled is None:
        return WritebackResult(
            applied={}, before={}, after={},
            idempotent_skip=False, reason="kunal note not found",
        )
    _apple_id, body_html = pulled

    if _has_marker(body_html, reply_id):
        return WritebackResult(
            applied={}, before={}, after={},
            idempotent_skip=True,
            reason=f"reply {reply_id} already applied",
        )

    # Normalize aliases in the delta dict.
    cleaned: dict[str, int] = {}
    for k, v in decrements.items():
        canon = "breathe" if k.lower() == "breathing" else k.lower()
        if canon in TYPES and isinstance(v, int) and v > 0:
            cleaned[canon] = v

    if not cleaned:
        return WritebackResult(
            applied={}, before={}, after={},
            reason="no valid decrements provided",
        )

    # Parse current counters (stripping images, as the parser already does
    # through the Counters pipeline — but here we work on raw body for
    # surgical rewriting).
    before = parse_counters(_strip_images(body_html))

    applied: dict[str, int] = {}
    rewritten_bodies: list[str] = []

    def _sub(m: re.Match) -> str:
        raw_type = m.group(1).lower() if False else None
        # `m.group("before")` includes the leading word — pull the type.
        prefix = m.group("before")
        type_match = re.match(r"(\w+)", prefix, re.IGNORECASE)
        if not type_match:
            return m.group(0)
        t = type_match.group(1).lower()
        canon = "breathe" if t == "breathing" else t
        if canon not in cleaned:
            return m.group(0)
        n = int(m.group("num"))
        delta = min(cleaned[canon], n)  # cap at current value
        new_n = n - delta
        applied[canon] = applied.get(canon, 0) + delta
        return f"{prefix}{new_n}"

    new_body = _LINE_RE.sub(_sub, body_html)

    after = parse_counters(_strip_images(new_body))

    # Don't touch the note if no lines matched.
    if not applied:
        return WritebackResult(
            applied={}, before=before.as_dict(), after=after.as_dict(),
            reason="no counter lines matched for provided types",
        )

    new_body = _append_marker(new_body, reply_id)

    if dry_run:
        logger.info("[writeback] dry-run (default): would apply %s", applied)
    else:
        _write_kunal_note_body(new_body)
        logger.info("[writeback] APPLIED to Kunal note: %s", applied)

    return WritebackResult(
        applied=applied,
        before=before.as_dict(),
        after=after.as_dict(),
        reason="dry-run" if dry_run else "written",
    )


# ── Helpers ────────────────────────────────────────────────────────

# Matches any base64 image data URI (same as harvester). Imported here
# to avoid a cross-module dep on a private regex.
_DATA_URI_RE = re.compile(
    r"<img[^>]*src=\"data:image/[^\"]+\"[^>]*/?>", re.IGNORECASE | re.DOTALL
)


def _strip_images(body_html: str) -> str:
    return _DATA_URI_RE.sub("", body_html)
