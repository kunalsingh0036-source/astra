"""Reusable, deterministic quality checks for drafted content.

Every check is a pure text function returning (ok: bool, detail: str) — no
LLM, no network — so they run in CI as the regression net AND get applied
to real LLM output in the live eval layer. A draft must pass the checks
relevant to its kind; a failing check is a regression to investigate, not
a stylistic nit.
"""

from __future__ import annotations

import re

Result = tuple[bool, str]


# ── Placeholders ────────────────────────────────────────────────────
# Both drafters forbid placeholders ([Name], [date], (insert X), [TBD…],
# [KUNAL: …]). A bracketed token or "(insert/your/tbd …)" is the tell. We
# deliberately do NOT exempt markdown links — drafted emails/posts have no
# business shipping `[text](url)` either.
_PLACEHOLDER_RE = re.compile(
    r"\[[^\]\n]{1,60}\]"  # [Name], [date], [KUNAL: ...], [TBD ...]
    r"|\((?:insert|your|name|date|tbd|company|title)\b[^)\n]{0,60}\)",
    re.I,
)


def no_placeholder(text: str) -> Result:
    m = _PLACEHOLDER_RE.search(text or "")
    return (m is None, f"placeholder: {m.group(0)!r}" if m else "no placeholders")


# ── AI / marketer tells ─────────────────────────────────────────────
_HEDGE = (
    "i hope this email finds you well",
    "i hope this finds you well",
    "thrilled to announce",
    "humbled to",
    "excited to share",
    "in today's fast-paced",
    "in today's ever-evolving",
    "as an ai",
    "as a language model",
    "i'm just an ai",
    "dear sir/ma'am",
    "to whom it may concern",
)


def no_ai_tells(text: str) -> Result:
    low = (text or "").lower()
    hits = [h for h in _HEDGE if h in low]
    return (not hits, f"AI/marketer tells: {hits}" if hits else "clean voice")


# ── Length ──────────────────────────────────────────────────────────
def within_chars(text: str, lo: int, hi: int) -> Result:
    n = len((text or "").strip())
    return (lo <= n <= hi, f"{n} chars (want {lo}–{hi})")


# ── LinkedIn shape ──────────────────────────────────────────────────
def has_hook(text: str) -> Result:
    first = (text or "").strip().split("\n", 1)[0].strip()
    ok = 0 < len(first) <= 120
    return (ok, f"hook {len(first)} chars" if first else "no hook line")


def has_hashtags(tags: list[str] | None, lo: int = 2, hi: int = 5) -> Result:
    n = len(tags or [])
    return (lo <= n <= hi, f"{n} hashtags (want {lo}–{hi})")


# ── Internal-state leak (delegates to the PROD guard) ───────────────
# Importing the real scanner means: if anyone weakens _leaks_internal, the
# KNOWN-leak golden case below fails — the guard can't silently rot.
def no_internal_leak(text: str) -> Result:
    from astra.creators.draft_linkedin_post import _leaks_internal

    hits = _leaks_internal(text or "")
    return (not hits, f"LEAKED: {sorted(set(hits))}" if hits else "no internal leak")


# ── Helpers ─────────────────────────────────────────────────────────
def run_named(checks: dict[str, Result]) -> tuple[bool, list[str]]:
    """Collapse a dict of name→Result into (all_ok, failure_lines)."""
    fails = [f"{name}: {detail}" for name, (ok, detail) in checks.items() if not ok]
    return (not fails, fails)
