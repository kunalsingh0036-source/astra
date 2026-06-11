"""
Timeout hierarchy assertions.

Reads every timeout in the system and verifies outer ≥ inner + 60s
margin. If anyone introduces a new timeout that violates the
hierarchy, this test fails before deploy.

Source of truth for what each value SHOULD be is
docs/timeout_hierarchy.md. If a number changes, both this test and
the doc must be updated together.

Phase 2b note: the chat path is polling, not SSE. The relevant
outer for the runner is no longer Vercel maxDuration (just an
enqueue call now, 10s) but chatPoller's maxPollDurationMs (10min) —
the browser-side ceiling that sits *outside* the runner. Vercel
maxDuration on /api/chat doesn't compete with runner anymore.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# Repo root, derived from this file's location — NOT the CWD and NOT
# an absolute laptop path. The hardcoded /Users/kunalsingh/... paths
# this replaced passed locally and failed in CI (FileNotFoundError),
# which the old soft-fail gate masked for weeks.
_ASTRA_ROOT = Path(__file__).resolve().parents[2]

# astra-web is a SEPARATE repo. On the laptop it's checked out as a
# sibling; in astra's CI it isn't checked out at all. Cross-repo
# hierarchy checks skip cleanly when it's absent — they still run
# everywhere the two repos coexist (laptop, any combined checkout).
_WEB_ROOT = _ASTRA_ROOT.parent / "astra-web"

requires_web = pytest.mark.skipif(
    not _WEB_ROOT.is_dir(),
    reason=f"astra-web not checked out at {_WEB_ROOT} — cross-repo "
    "timeout checks only run where both repos coexist",
)


def _read_constant(module_path: str, constant: str) -> int:
    """Read a top-level integer constant from a module without
    executing import side effects (which would fire DB connections,
    etc.). Uses ast for safety."""
    import ast
    source = open(module_path).read()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == constant:
                    if isinstance(node.value, ast.Constant) and isinstance(
                        node.value.value, (int, float)
                    ):
                        return int(node.value.value)
    raise AssertionError(f"constant {constant!r} not found in {module_path}")


# ── Timeout values pulled from the codebase ───────────────


def _vercel_chat_max_duration() -> int:
    # In TS — read by string match for the export const. With
    # Phase 2b, this is the enqueue-only timeout (10s). It no
    # longer governs the runner's lifetime.
    text = (_WEB_ROOT / "app/api/chat/route.ts").read_text()
    import re
    m = re.search(r"export const maxDuration\s*=\s*(\d+)", text)
    assert m, "maxDuration not found in /api/chat route"
    return int(m.group(1))


def _runner_per_turn_hard() -> int:
    return _read_constant(
        str(_ASTRA_ROOT / "astra/runtime/agent_loop.py"),
        "_TURN_HARD_TIMEOUT_SEC",
    )


def _browser_watchdog_ms() -> int:
    text = (_WEB_ROOT / "components/ChatProvider.tsx").read_text()
    import re
    m = re.search(r"const WATCHDOG_MS\s*=\s*([\d_]+)", text)
    assert m, "WATCHDOG_MS not found in ChatProvider"
    return int(m.group(1).replace("_", ""))


def _chat_poller_max_duration_ms() -> int:
    """chatPoller's hard cap on total polling duration. The browser
    stops polling after this; nothing observes terminal events
    after that. This is the actual outer bound on a turn from
    the user's POV."""
    text = (_WEB_ROOT / "lib/chatPoller.ts").read_text()
    import re
    # const DEFAULT_MAX_DURATION_MS = 10 * 60 * 1000;
    m = re.search(
        r"DEFAULT_MAX_DURATION_MS\s*=\s*([\d_]+)\s*\*\s*([\d_]+)\s*\*\s*([\d_]+)",
        text,
    )
    if m:
        return (
            int(m.group(1).replace("_", ""))
            * int(m.group(2).replace("_", ""))
            * int(m.group(3).replace("_", ""))
        )
    # Fallback: literal millis
    m = re.search(r"DEFAULT_MAX_DURATION_MS\s*=\s*([\d_]+)", text)
    assert m, "DEFAULT_MAX_DURATION_MS not found in chatPoller.ts"
    return int(m.group(1).replace("_", ""))


# ── Tests ─────────────────────────────────────────────────


@requires_web
def test_runner_under_poll_cap_with_margin() -> None:
    """Runner must finish + write terminal status BEFORE the
    browser's poll cap fires. Otherwise the user sees 'polling
    exceeded' while the runner is still legitimately working.

    With polling, the runner is a server-side asyncio.Task and the
    only browser-side cap on observation is chatPoller's
    DEFAULT_MAX_DURATION_MS. 60s margin holds the standard
    invariant.
    """
    runner_sec = _runner_per_turn_hard()
    poll_cap_sec = _chat_poller_max_duration_ms() // 1000
    margin = poll_cap_sec - runner_sec
    assert margin >= 60, (
        f"runner per-turn ({runner_sec}s) too close to chatPoller "
        f"maxPollDurationMs ({poll_cap_sec}s) — margin {margin}s, "
        f"need ≥60s. Either reduce runner or raise the poll cap."
    )


@requires_web
def test_browser_watchdog_above_runner() -> None:
    """The browser's stall watchdog (no events for N seconds)
    must NOT fire before the runner's hard cap. Otherwise a slow
    but legitimate turn (long tool call running quietly) looks
    dead from the browser's POV.
    """
    runner_sec = _runner_per_turn_hard()
    watchdog_sec = _browser_watchdog_ms() // 1000
    margin = watchdog_sec - runner_sec
    # 30s here — the watchdog is a safety net, not the primary
    # cap. Adaptive backoff means most idle stretches don't hit
    # the watchdog at all.
    assert margin >= 30, (
        f"browser watchdog ({watchdog_sec}s) needs ≥30s margin over "
        f"runner per-turn ({runner_sec}s); current margin {margin}s. "
        f"Raise WATCHDOG_MS in ChatProvider.tsx."
    )


@requires_web
def test_runner_smaller_than_browser_watchdog() -> None:
    """Runner must finish before browser gives up. Otherwise a slow-
    but-valid turn looks dead from the browser's POV."""
    runner_sec = _runner_per_turn_hard()
    watchdog_sec = _browser_watchdog_ms() // 1000
    assert runner_sec < watchdog_sec, (
        f"runner ({runner_sec}s) should be less than browser watchdog "
        f"({watchdog_sec}s) — otherwise legitimate slow turns get "
        f"declared dead before the runner finishes."
    )


@requires_web
def test_chat_post_under_runner() -> None:
    """The /api/chat enqueue call must return way faster than a
    full turn. With Phase 2b, /api/chat just spawns the asyncio
    task and returns turn_id — anything more than ~10s means
    the upstream /turns/start is hung.
    """
    chat_sec = _vercel_chat_max_duration()
    runner_sec = _runner_per_turn_hard()
    assert chat_sec < runner_sec, (
        f"/api/chat maxDuration ({chat_sec}s) should be far below "
        f"runner per-turn ({runner_sec}s) — /api/chat is supposed to "
        f"return immediately after enqueueing the turn."
    )


@requires_web
def test_documented_values_match_code() -> None:
    """If anyone updates the hierarchy doc without updating the
    code (or vice versa), this test catches the drift."""
    doc_text = (_ASTRA_ROOT / "docs/timeout_hierarchy.md").read_text()
    runner_sec = _runner_per_turn_hard()
    chat_sec = _vercel_chat_max_duration()
    watchdog_sec = _browser_watchdog_ms() // 1000
    poll_cap_sec = _chat_poller_max_duration_ms() // 1000
    # Spot-check that the documented numbers appear somewhere in
    # the doc. Not a full parse — that's overkill — but enough to
    # catch mass drift.
    assert f"{runner_sec}s" in doc_text or f"**{runner_sec}s**" in doc_text, (
        f"runner per-turn ({runner_sec}s) not mentioned in "
        f"docs/timeout_hierarchy.md — update the doc"
    )
    assert f"{chat_sec}s" in doc_text or f"**{chat_sec}s**" in doc_text, (
        f"/api/chat maxDuration ({chat_sec}s) not mentioned in doc"
    )
    assert (
        f"{watchdog_sec}s" in doc_text or f"**{watchdog_sec}s**" in doc_text
    ), f"browser watchdog ({watchdog_sec}s) not mentioned in doc"
    assert (
        f"{poll_cap_sec}s" in doc_text or f"**{poll_cap_sec}s**" in doc_text
        or f"{poll_cap_sec // 60} min" in doc_text
        or f"({poll_cap_sec // 60} min)" in doc_text
    ), f"chatPoller maxPollDurationMs ({poll_cap_sec}s) not mentioned in doc"
