"""
Timeout hierarchy assertions.

Reads every timeout in the system and verifies outer ≥ inner + 60s
margin. If anyone introduces a new timeout that violates the
hierarchy, this test fails before deploy.

Source of truth for what each value SHOULD be is
docs/timeout_hierarchy.md. If a number changes, both this test and
the doc must be updated together.
"""

from __future__ import annotations


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
    # In TS — read by string match for the export const
    text = open(
        "/Users/kunalsingh/Claude Code/astra-web/app/api/chat/route.ts"
    ).read()
    import re
    m = re.search(r"export const maxDuration\s*=\s*(\d+)", text)
    assert m, "maxDuration not found in /api/chat route"
    return int(m.group(1))


def _runner_per_turn_hard() -> int:
    return _read_constant(
        "astra/runtime/agent_loop.py", "_TURN_HARD_TIMEOUT_SEC"
    )


def _browser_watchdog_ms() -> int:
    text = open(
        "/Users/kunalsingh/Claude Code/astra-web/components/ChatProvider.tsx"
    ).read()
    import re
    m = re.search(r"const WATCHDOG_MS\s*=\s*([\d_]+)", text)
    assert m, "WATCHDOG_MS not found in ChatProvider"
    return int(m.group(1).replace("_", ""))


# ── Tests ─────────────────────────────────────────────────


def test_runner_under_vercel_with_margin() -> None:
    """Runner must finish + emit terminal event before Vercel kills
    the connection. 60s margin minimum."""
    runner = _runner_per_turn_hard()
    vercel = _vercel_chat_max_duration()
    margin = vercel - runner
    assert margin >= 60, (
        f"runner per-turn ({runner}s) too close to Vercel maxDuration "
        f"({vercel}s) — margin {margin}s, need ≥60s. Reduce runner "
        f"or raise Vercel."
    )


def test_browser_watchdog_above_vercel_with_margin() -> None:
    """Browser watchdog must NOT fire before Vercel — otherwise we
    declare 'stream dead' while the request is still legitimately
    in flight upstream."""
    browser_sec = _browser_watchdog_ms() // 1000
    vercel = _vercel_chat_max_duration()
    # Tight margin tolerated here (30s) since browser watchdog is
    # only a safety net; the synthetic terminal-event in
    # chatStream.ts handles clean cancellations within seconds.
    # When polling lands (Phase 2b), this whole layer disappears.
    margin = browser_sec - vercel
    assert margin >= 30, (
        f"browser watchdog ({browser_sec}s) needs ≥30s margin over "
        f"Vercel maxDuration ({vercel}s); current margin {margin}s. "
        f"Raise WATCHDOG_MS in ChatProvider.tsx."
    )


def test_runner_smaller_than_browser_watchdog() -> None:
    """Runner must finish before browser gives up. Otherwise a slow-
    but-valid turn looks dead from the browser's POV."""
    runner = _runner_per_turn_hard()
    browser_sec = _browser_watchdog_ms() // 1000
    assert runner < browser_sec, (
        f"runner ({runner}s) should be less than browser watchdog "
        f"({browser_sec}s) — otherwise legitimate slow turns get "
        f"declared dead before the runner finishes."
    )


def test_documented_values_match_code() -> None:
    """If anyone updates the hierarchy doc without updating the
    code (or vice versa), this test catches the drift."""
    doc_text = open(
        "/Users/kunalsingh/Claude Code/astra/docs/timeout_hierarchy.md"
    ).read()
    runner = _runner_per_turn_hard()
    vercel = _vercel_chat_max_duration()
    browser_sec = _browser_watchdog_ms() // 1000
    # Spot-check that the documented numbers appear somewhere in the
    # doc. Not a full parse — that's overkill — but enough to catch
    # mass drift.
    assert f"{runner}s" in doc_text or f"**{runner}s**" in doc_text, (
        f"runner per-turn ({runner}s) not mentioned in "
        f"docs/timeout_hierarchy.md — update the doc"
    )
    assert f"{vercel}s" in doc_text or f"**{vercel}s**" in doc_text, (
        f"Vercel maxDuration ({vercel}s) not mentioned in doc"
    )
    assert (
        f"{browser_sec}s" in doc_text or f"**{browser_sec}s**" in doc_text
    ), f"browser watchdog ({browser_sec}s) not mentioned in doc"
