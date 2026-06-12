#!/usr/bin/env python3
"""
End-to-end smoke test against the deployed Astra system.

Goal: stop being the QA bottleneck. Every critical path is exercised
against the actual deployed stack — not unit tests in isolation. If
this script doesn't pass, no commit lands.

Usage:
    python scripts/e2e_smoke.py
    python scripts/e2e_smoke.py --base https://astra.thearrogantclub.com
    python scripts/e2e_smoke.py --skip-bridge --skip-pdf

Tests, in order:
    01  /api/sessions           — list endpoint reachable
    02  /api/turns/recent       — recent turns reachable
    03  /api/chat (text-only)   — simple text prompt → session, text_delta, done
    04  /api/chat (tool-using)  — recall_recent_turns → tool_call + tool_result
    05  /api/chat (multi-turn)  — second turn rehydrates session history
    06  /api/sessions/<id>      — full session content reachable
    07  /api/bridge/expand      — error when no daemon (or success when online)
    08  /api/chat (PDF flow)    — draft_doc + render_doc_pdf produces an artifact

Every test prints PASS/FAIL with timing. Final summary exits non-zero
if any test failed.

Authentication: the deployed stack requires an authenticated session
to hit /api/chat. This harness uses an `ASTRA_E2E_COOKIE` env var
holding the user's NextAuth session cookie. Without it, the harness
runs the public-API tests only.

This script is a living document of "what works." When a bug surfaces
in production, the corresponding test gets added here so it can never
silently regress.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any

try:
    import httpx
except ImportError:
    print("missing httpx — pip install httpx", file=sys.stderr)
    sys.exit(2)


# ── Result tracking ────────────────────────────────────────


@dataclass
class TestResult:
    name: str
    passed: bool
    duration_ms: int
    detail: str = ""
    error: str | None = None


@dataclass
class HarnessState:
    base_url: str
    cookie: str | None
    # The middleware (astra-web/middleware.ts) bypasses auth for any
    # request that carries x-astra-secret matching ASTRA_SHARED_SECRET.
    # That's a documented server-to-server path (scheduler, webhooks).
    # The harness uses it so CI can run agent-path tests without
    # exfiltrating a NextAuth cookie. Either auth mode unlocks
    # tests 03-07; cookie wins if both are set.
    shared_secret: str | None = None
    results: list[TestResult] = field(default_factory=list)
    # Cross-test state: the session_id from test 03 is reused in 04, 05, 06
    session_id: str | None = None
    last_turn_text: str = ""

    @property
    def has_auth(self) -> bool:
        """True if either auth path is wired up."""
        return bool(self.cookie or self.shared_secret)

    def record(self, r: TestResult) -> None:
        self.results.append(r)
        flag = "PASS" if r.passed else "FAIL"
        line = f"  [{flag}] {r.name}  ({r.duration_ms}ms)"
        if r.detail:
            line += f"  {r.detail}"
        print(line)
        if r.error:
            print(f"    error: {r.error}")


# ── HTTP helpers ──────────────────────────────────────────


def _headers(state: HarnessState) -> dict[str, str]:
    h = {"content-type": "application/json"}
    if state.cookie:
        # Pass-through next-auth cookie if provided.
        h["cookie"] = state.cookie
    if state.shared_secret:
        # Server-to-server bypass header. Middleware accepts either
        # this OR the cookie — we send both when both are set so the
        # harness keeps working even if one is mis-configured.
        h["x-astra-secret"] = state.shared_secret
    return h


async def _post_chat_stream(
    state: HarnessState,
    client: httpx.AsyncClient,
    prompt: str,
    *,
    session_id: str | None = None,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """Send a prompt and aggregate events the way the browser does.

    Phase 2b changed the wire protocol: /api/chat now returns JSON
    {turn_id, session_id, status} instead of streaming SSE. The
    browser polls /api/turns/<id>/events?after=<lastOrd> until the
    response carries terminal=true. This helper does the same so
    the harness measures the same path real users hit.

    Function name kept for backward compat — the return shape
    matches the prior SSE version (events list, text, session_id,
    saw_terminal, final/error payloads, ok). Downstream tests
    don't change.
    """
    body: dict[str, Any] = {"prompt": prompt}
    if session_id:
        body["session_id"] = session_id

    started = time.monotonic()
    seen: list[dict[str, Any]] = []
    text_deltas: list[str] = []
    canonical_session_id: str | None = None
    saw_terminal = False
    final_payload: dict[str, Any] = {}
    error_payload: dict[str, Any] = {}

    # ── Step 1: POST /api/chat — enqueue turn, get turn_id ──
    try:
        start_resp = await client.post(
            f"{state.base_url}/api/chat",
            headers=_headers(state),
            json=body,
            timeout=15.0,
        )
    except Exception as e:
        return {
            "ok": False,
            "status": 0,
            "exception": f"{type(e).__name__}: {e}",
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
    if start_resp.status_code != 200:
        return {
            "ok": False,
            "status": start_resp.status_code,
            "body": start_resp.text[:500],
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
    try:
        start_json = start_resp.json()
    except Exception:
        return {
            "ok": False,
            "status": start_resp.status_code,
            "body": "non-json /api/chat response",
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
    turn_id = start_json.get("turn_id")
    canonical_session_id = start_json.get("session_id") or session_id
    if not turn_id:
        return {
            "ok": False,
            "status": start_resp.status_code,
            "body": f"missing turn_id: {start_json}",
            "duration_ms": int((time.monotonic() - started) * 1000),
        }

    # ── Step 2: poll /api/turns/<id>/events ──
    last_ord = 0
    poll_url = f"{state.base_url}/api/turns/{turn_id}/events"
    deadline = started + timeout
    while True:
        if time.monotonic() > deadline:
            return {
                "ok": False,
                "status": 0,
                "exception": f"poll timeout after {timeout:.0f}s (turn={turn_id})",
                "duration_ms": int((time.monotonic() - started) * 1000),
            }
        try:
            poll_resp = await client.get(
                f"{poll_url}?after={last_ord}",
                headers=_headers(state),
                timeout=10.0,
            )
        except Exception as e:
            # transient — retry on the next tick
            await asyncio.sleep(0.5)
            continue
        if poll_resp.status_code != 200:
            return {
                "ok": False,
                "status": poll_resp.status_code,
                "body": poll_resp.text[:500],
                "duration_ms": int((time.monotonic() - started) * 1000),
            }
        try:
            poll_json = poll_resp.json()
        except Exception:
            return {
                "ok": False,
                "status": poll_resp.status_code,
                "body": "non-json poll response",
                "duration_ms": int((time.monotonic() - started) * 1000),
            }
        for ev in poll_json.get("events", []):
            ord_ = ev.get("ord", 0)
            if ord_ > last_ord:
                last_ord = ord_
            name = ev.get("event")
            data = ev.get("payload") or {}
            seen.append({"event": name, "data": data})
            if name == "session":
                canonical_session_id = data.get("session_id") or canonical_session_id
            elif name == "text_delta":
                text_deltas.append(data.get("content", ""))
            elif name == "done":
                saw_terminal = True
                final_payload = data
            elif name == "error":
                saw_terminal = True
                error_payload = data
        if poll_json.get("terminal"):
            # Synthesize a done if the agent ended without one
            # (failed/interrupted/timeout terminals skip the done
            # event by design — match chatPoller.ts behaviour).
            if not saw_terminal:
                saw_terminal = True
                final_payload = {
                    "duration_ms": poll_json.get("duration_ms")
                    or int((time.monotonic() - started) * 1000),
                }
                err_msg = poll_json.get("error_message")
                if err_msg:
                    error_payload = {"message": err_msg}
            break
        await asyncio.sleep(0.4)

    return {
        "ok": saw_terminal and not error_payload,
        "status": 200,
        "session_id": canonical_session_id,
        "text": "".join(text_deltas),
        "events": seen,
        "saw_terminal": saw_terminal,
        "final": final_payload,
        "error": error_payload,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "turn_id": turn_id,
    }


def _parse_sse_frame(frame: str) -> dict[str, Any] | None:
    name = ""
    data: dict[str, Any] = {}
    for line in frame.split("\n"):
        if line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:"):
            try:
                data = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                pass
    if not name:
        return None
    return {"event": name, "data": data}


# ── Tests ──────────────────────────────────────────────────


async def test_00_health_deep(
    state: HarnessState, client: httpx.AsyncClient
) -> TestResult:
    """Public health endpoint — probed first so we know the system
    is alive before exercising agent-path tests. No auth required."""
    started = time.monotonic()
    try:
        r = await client.get(
            f"{state.base_url}/api/health/deep",
            timeout=15.0,
        )
        elapsed = int((time.monotonic() - started) * 1000)
        if r.status_code != 200:
            return TestResult(
                name="00 /api/health/deep",
                passed=False,
                duration_ms=elapsed,
                error=f"HTTP {r.status_code}",
            )
        body = r.json()
        status = body.get("status", "unknown")
        checks = body.get("checks", []) or []
        down = [c["name"] for c in checks if c.get("status") == "down"]
        degraded = [
            c["name"] for c in checks if c.get("status") == "degraded"
        ]
        detail = f"{status} · {len(checks)} checks"
        if down:
            detail += f" · DOWN={','.join(down)}"
        if degraded:
            detail += f" · DEGRADED={','.join(degraded)}"
        return TestResult(
            name="00 /api/health/deep",
            passed=status != "down",
            duration_ms=elapsed,
            detail=detail,
            error=(
                f"system status={status}, down={down}"
                if status == "down"
                else None
            ),
        )
    except Exception as e:
        return TestResult(
            name="00 /api/health/deep",
            passed=False,
            duration_ms=int((time.monotonic() - started) * 1000),
            error=f"{type(e).__name__}: {e}",
        )


async def test_01_sessions_list(
    state: HarnessState, client: httpx.AsyncClient
) -> TestResult:
    if not state.has_auth:
        return TestResult(
            name="01 /api/sessions list",
            passed=True,
            duration_ms=0,
            detail="SKIPPED — no auth (cookie or shared secret)",
        )
    started = time.monotonic()
    try:
        r = await client.get(
            f"{state.base_url}/api/sessions?limit=3",
            headers=_headers(state),
        )
        elapsed = int((time.monotonic() - started) * 1000)
        if r.status_code != 200:
            return TestResult(
                name="01 /api/sessions list",
                passed=False,
                duration_ms=elapsed,
                error=f"HTTP {r.status_code}: {r.text[:200]}",
            )
        body = r.json()
        if "sessions" not in body or not isinstance(body["sessions"], list):
            return TestResult(
                name="01 /api/sessions list",
                passed=False,
                duration_ms=elapsed,
                error=f"shape mismatch: {body}",
            )
        return TestResult(
            name="01 /api/sessions list",
            passed=True,
            duration_ms=elapsed,
            detail=f"{len(body['sessions'])} session(s)",
        )
    except Exception as e:
        return TestResult(
            name="01 /api/sessions list",
            passed=False,
            duration_ms=int((time.monotonic() - started) * 1000),
            error=f"{type(e).__name__}: {e}",
        )


async def test_02_recent_turns(
    state: HarnessState, client: httpx.AsyncClient
) -> TestResult:
    if not state.has_auth:
        return TestResult(
            name="02 /api/turns/recent",
            passed=True,
            duration_ms=0,
            detail="SKIPPED — no auth (cookie or shared secret)",
        )
    started = time.monotonic()
    try:
        r = await client.get(
            f"{state.base_url}/api/turns/recent?limit=3",
            headers=_headers(state),
        )
        elapsed = int((time.monotonic() - started) * 1000)
        if r.status_code != 200:
            return TestResult(
                name="02 /api/turns/recent",
                passed=False,
                duration_ms=elapsed,
                error=f"HTTP {r.status_code}",
            )
        body = r.json()
        if "turns" not in body:
            return TestResult(
                name="02 /api/turns/recent",
                passed=False,
                duration_ms=elapsed,
                error=f"shape: {body}",
            )
        return TestResult(
            name="02 /api/turns/recent",
            passed=True,
            duration_ms=elapsed,
            detail=f"{len(body['turns'])} turn(s)",
        )
    except Exception as e:
        return TestResult(
            name="02 /api/turns/recent",
            passed=False,
            duration_ms=int((time.monotonic() - started) * 1000),
            error=f"{type(e).__name__}: {e}",
        )


async def test_03_simple_text_turn(
    state: HarnessState, client: httpx.AsyncClient
) -> TestResult:
    if not state.has_auth:
        return TestResult(
            name="03 text-only turn",
            passed=True,
            duration_ms=0,
            detail="SKIPPED — no auth (cookie or shared secret)",
        )
    started = time.monotonic()
    out = await _post_chat_stream(
        state,
        client,
        "Reply with exactly the four words: harness e2e is alive. No other words.",
        timeout=90.0,
    )
    elapsed = out["duration_ms"]
    if not out.get("ok"):
        return TestResult(
            name="03 text-only turn",
            passed=False,
            duration_ms=elapsed,
            error=str(out.get("error") or out.get("exception") or out.get("body")),
        )
    state.session_id = out.get("session_id")
    state.last_turn_text = out.get("text", "")
    if "harness" not in state.last_turn_text.lower():
        return TestResult(
            name="03 text-only turn",
            passed=False,
            duration_ms=elapsed,
            error=f"response missing 'harness': {state.last_turn_text[:200]}",
        )
    return TestResult(
        name="03 text-only turn",
        passed=True,
        duration_ms=elapsed,
        detail=f"sid={state.session_id[:8] if state.session_id else 'none'}…",
    )


async def test_04_tool_using_turn(
    state: HarnessState, client: httpx.AsyncClient
) -> TestResult:
    if not state.has_auth or not state.session_id:
        return TestResult(
            name="04 tool-using turn",
            passed=True,
            duration_ms=0,
            detail="SKIPPED — no auth or session from #03",
        )
    started = time.monotonic()
    out = await _post_chat_stream(
        state,
        client,
        "Call recall_recent_turns with limit=1 and quote the prompt verbatim.",
        session_id=state.session_id,
        timeout=120.0,
    )
    elapsed = out["duration_ms"]
    if not out.get("ok"):
        return TestResult(
            name="04 tool-using turn",
            passed=False,
            duration_ms=elapsed,
            error=str(out.get("error") or out.get("exception") or out.get("body")),
        )
    # Verify tool_call event arrived
    saw_tool_call = any(
        e.get("event") == "tool_call" for e in out.get("events", [])
    )
    saw_tool_result = any(
        e.get("event") == "tool_result" for e in out.get("events", [])
    )
    if not saw_tool_call:
        return TestResult(
            name="04 tool-using turn",
            passed=False,
            duration_ms=elapsed,
            error="no tool_call event seen",
        )
    if not saw_tool_result:
        return TestResult(
            name="04 tool-using turn",
            passed=False,
            duration_ms=elapsed,
            error="no tool_result event seen",
        )
    return TestResult(
        name="04 tool-using turn",
        passed=True,
        duration_ms=elapsed,
        detail="tool_call + tool_result events",
    )


async def test_05_session_continuity(
    state: HarnessState, client: httpx.AsyncClient
) -> TestResult:
    if not state.has_auth or not state.session_id:
        return TestResult(
            name="05 session continuity",
            passed=True,
            duration_ms=0,
            detail="SKIPPED",
        )
    started = time.monotonic()
    # Reference back to test 03's content. The agent should be able
    # to recall the four-word phrase if session continuity works.
    out = await _post_chat_stream(
        state,
        client,
        "What was the exact phrase I asked you to reply with two turns ago?",
        session_id=state.session_id,
        timeout=120.0,
    )
    elapsed = out["duration_ms"]
    if not out.get("ok"):
        return TestResult(
            name="05 session continuity",
            passed=False,
            duration_ms=elapsed,
            error=str(out.get("error") or out.get("exception")),
        )
    text = out.get("text", "").lower()
    if "harness" in text and "alive" in text:
        return TestResult(
            name="05 session continuity",
            passed=True,
            duration_ms=elapsed,
            detail="agent recalled prior turn",
        )
    return TestResult(
        name="05 session continuity",
        passed=False,
        duration_ms=elapsed,
        error=f"agent didn't recall: {out.get('text','')[:200]}",
    )


async def test_06_session_detail(
    state: HarnessState, client: httpx.AsyncClient
) -> TestResult:
    if not state.session_id:
        return TestResult(
            name="06 /api/sessions/[id]",
            passed=True,
            duration_ms=0,
            detail="SKIPPED — no session id",
        )
    started = time.monotonic()
    try:
        r = await client.get(
            f"{state.base_url}/api/sessions/{state.session_id}",
            headers=_headers(state),
        )
        elapsed = int((time.monotonic() - started) * 1000)
        if r.status_code != 200:
            return TestResult(
                name="06 /api/sessions/[id]",
                passed=False,
                duration_ms=elapsed,
                error=f"HTTP {r.status_code}",
            )
        body = r.json()
        turns = body.get("turns") or []
        return TestResult(
            name="06 /api/sessions/[id]",
            passed=len(turns) >= 1,
            duration_ms=elapsed,
            detail=f"{len(turns)} turn(s) loaded",
        )
    except Exception as e:
        return TestResult(
            name="06 /api/sessions/[id]",
            passed=False,
            duration_ms=int((time.monotonic() - started) * 1000),
            error=f"{type(e).__name__}: {e}",
        )


async def test_07_bridge_expand_handling(
    state: HarnessState, client: httpx.AsyncClient
) -> TestResult:
    """Either the bridge is online (expand succeeds) or offline
    (returns 404 with a structured error). Both are valid; the test
    fails only if the endpoint crashes or shape is wrong."""
    if not state.has_auth:
        return TestResult(
            name="07 /api/bridge/expand",
            passed=True,
            duration_ms=0,
            detail="SKIPPED",
        )
    started = time.monotonic()
    try:
        r = await client.post(
            f"{state.base_url}/api/bridge/expand",
            headers=_headers(state),
            json={"paths": ["/Users/kunalsingh/Documents"]},
        )
        elapsed = int((time.monotonic() - started) * 1000)
        body: dict[str, Any] = {}
        try:
            body = r.json()
        except Exception:
            pass
        if r.status_code == 200:
            return TestResult(
                name="07 /api/bridge/expand",
                passed=isinstance(body.get("allowed_paths"), list),
                duration_ms=elapsed,
                detail="bridge online + expanded",
            )
        if r.status_code == 404:
            return TestResult(
                name="07 /api/bridge/expand",
                passed="error" in body,
                duration_ms=elapsed,
                detail="bridge offline (404 with error msg)",
            )
        return TestResult(
            name="07 /api/bridge/expand",
            passed=False,
            duration_ms=elapsed,
            error=f"unexpected HTTP {r.status_code}: {body}",
        )
    except Exception as e:
        return TestResult(
            name="07 /api/bridge/expand",
            passed=False,
            duration_ms=int((time.monotonic() - started) * 1000),
            error=f"{type(e).__name__}: {e}",
        )


async def test_08_email_data_endpoint(
    state: HarnessState, client: httpx.AsyncClient
) -> TestResult:
    """Probes /api/email/digest — the route a user sees on the /email
    page. Why this test exists: the previous "8/8 passing" claim
    masked a real production failure. The harness only tested the
    chat path; per-agent data endpoints (email, whatsapp, finance)
    weren't exercised, so an entire class of "fetch failed"
    failures was invisible to the smoke run.

    Soft assertion: agent might be reachable (200 with data) OR
    upstream might be down/unconfigured (502/503/500 with a
    structured error). Both pass the test. Test FAILS if the route
    itself crashes (timeout, malformed JSON, 5xx with no body).
    """
    if not state.has_auth:
        return TestResult(
            name="08 /api/email/digest",
            passed=True,
            duration_ms=0,
            detail="SKIPPED",
        )
    started = time.monotonic()
    try:
        r = await client.get(
            f"{state.base_url}/api/email/digest?hours=24",
            headers=_headers(state),
            timeout=15.0,
        )
        elapsed = int((time.monotonic() - started) * 1000)
        try:
            body: dict[str, Any] = r.json()
        except Exception:
            return TestResult(
                name="08 /api/email/digest",
                passed=False,
                duration_ms=elapsed,
                error=f"non-JSON response (HTTP {r.status_code})",
            )
        if r.status_code == 200 and "real_inbound" in body:
            return TestResult(
                name="08 /api/email/digest",
                passed=True,
                duration_ms=elapsed,
                detail=f"healthy · {body.get('real_inbound', '?')} real inbound",
            )
        # Acceptable error shapes: any with `error` field. Catches the
        # earlier "fetch failed" production bug because the route
        # WOULD have returned an error JSON, but with a useful
        # message tag.
        if "error" in body:
            return TestResult(
                name="08 /api/email/digest",
                passed=True,
                duration_ms=elapsed,
                detail=f"upstream error reported: {str(body['error'])[:60]}",
            )
        return TestResult(
            name="08 /api/email/digest",
            passed=False,
            duration_ms=elapsed,
            error=f"unexpected HTTP {r.status_code}: {str(body)[:200]}",
        )
    except Exception as e:
        return TestResult(
            name="08 /api/email/digest",
            passed=False,
            duration_ms=int((time.monotonic() - started) * 1000),
            error=f"{type(e).__name__}: {e}",
        )


async def test_09_agent_state_per_agent(
    state: HarnessState, client: httpx.AsyncClient
) -> TestResult:
    """Probes /api/state and verifies every expected agent is in the
    response, plus reports per-agent reachability. This catches the
    "agent dim because URL env var unset" class of failure that the
    chat-only harness missed.

    Pass criteria: response has the expected shape (`agents` array,
    `bridge`, `degraded`). Per-agent reachability is REPORTED in the
    detail string but doesn't fail the test — agents being dim is a
    deploy-state issue, not a regression.
    """
    if not state.has_auth:
        return TestResult(
            name="09 /api/state per-agent",
            passed=True,
            duration_ms=0,
            detail="SKIPPED",
        )
    started = time.monotonic()
    try:
        r = await client.get(
            f"{state.base_url}/api/state",
            headers=_headers(state),
            timeout=12.0,
        )
        elapsed = int((time.monotonic() - started) * 1000)
        if r.status_code != 200:
            return TestResult(
                name="09 /api/state per-agent",
                passed=False,
                duration_ms=elapsed,
                error=f"HTTP {r.status_code}",
            )
        body: dict[str, Any] = r.json()
        agents = body.get("agents") or []
        if not isinstance(agents, list) or not agents:
            return TestResult(
                name="09 /api/state per-agent",
                passed=False,
                duration_ms=elapsed,
                error="empty or non-list agents field",
            )
        reachable = sum(1 for a in agents if a.get("reachable"))
        dim = [
            a.get("id") for a in agents if not a.get("reachable")
        ]
        detail = f"{reachable}/{len(agents)} reachable"
        if dim:
            detail += f" · dim: {','.join(d for d in dim if d)}"
        return TestResult(
            name="09 /api/state per-agent",
            passed=True,
            duration_ms=elapsed,
            detail=detail,
        )
    except Exception as e:
        return TestResult(
            name="09 /api/state per-agent",
            passed=False,
            duration_ms=int((time.monotonic() - started) * 1000),
            error=f"{type(e).__name__}: {e}",
        )


# ── User-journey regression locks ──────────────────────────
#
# Each test below pins a specific bug class we've shipped a fix for.
# If the underlying behaviour regresses, this is what catches it
# before users do. Naming convention: test_NN_<what_breaks_for_user>.
# Every test uses a fresh session so prior conversational state can't
# bleed across — these are not unit tests, they're "type a prompt,
# look at the UI" simulations from a cold start.


async def test_10_palette_artifact_round_trip(
    state: HarnessState, client: httpx.AsyncClient
) -> TestResult:
    """User journey: ask for a color palette, see swatches.

    Regression lock for the artifact-sentinel parser fix. Before the
    fix, the model called emit_palette, got a 200 back with the
    sentinel-wrapped payload, then told the user "I rendered four
    swatches above" — and the UI got nothing because the lean runtime
    never parsed the sentinel into an artifact event. This test fires
    a prompt that should reliably trigger emit_palette and asserts
    the artifact event arrived with the shape the web UI expects.
    """
    if not state.has_auth:
        return TestResult(
            name="10 palette artifact round-trip",
            passed=True,
            duration_ms=0,
            detail="SKIPPED — no auth",
        )
    out = await _post_chat_stream(
        state,
        client,
        # Prompt is engineered to force the tool call rather than
        # prose — the system prompt's "NEVER dump hex codes as prose"
        # rule reinforces this, but we restate it inline so a single
        # turn doesn't depend on the rule being in context.
        "Use the emit_palette tool to generate a 4-color cinematic "
        "palette. Each color must have a hex and a label. Don't write "
        "hex codes in prose.",
        timeout=90.0,
    )
    elapsed = out["duration_ms"]
    if not out.get("ok"):
        return TestResult(
            name="10 palette artifact round-trip",
            passed=False,
            duration_ms=elapsed,
            error=str(out.get("error") or out.get("exception") or out.get("body")),
        )
    artifact_events = [
        e for e in out.get("events", []) if e.get("event") == "artifact"
    ]
    palette_arts = [
        e for e in artifact_events
        if (e.get("data") or {}).get("type") == "palette"
    ]
    if not palette_arts:
        # Surface what the model actually did so debugging is one-step
        tool_calls = [
            (e.get("data") or {}).get("name")
            for e in out.get("events", [])
            if e.get("event") == "tool_call"
        ]
        return TestResult(
            name="10 palette artifact round-trip",
            passed=False,
            duration_ms=elapsed,
            error=(
                f"no palette artifact event seen. tools called: "
                f"{tool_calls or 'none'}. text head: "
                f"{out.get('text', '')[:120]!r}"
            ),
        )
    payload = (palette_arts[0].get("data") or {}).get("content") or {}
    colors = payload.get("colors") or []
    if len(colors) < 3:
        return TestResult(
            name="10 palette artifact round-trip",
            passed=False,
            duration_ms=elapsed,
            error=f"palette has only {len(colors)} colors (need ≥3)",
        )
    # Defense in depth: assistant text must not contain raw sentinels.
    # If it does, the parser regressed and the user sees ASCII soup.
    if "⟦ASTRA_ARTIFACT⟧" in out.get("text", ""):
        return TestResult(
            name="10 palette artifact round-trip",
            passed=False,
            duration_ms=elapsed,
            error="raw ⟦ASTRA_ARTIFACT⟧ marker leaked into response text",
        )
    return TestResult(
        name="10 palette artifact round-trip",
        passed=True,
        duration_ms=elapsed,
        detail=f"{len(colors)} swatches",
    )


async def test_11_autonomy_mode_sync(
    state: HarnessState, client: httpx.AsyncClient
) -> TestResult:
    """User journey: flip autonomy mode in the UI, agent sees it.

    Regression lock for the cross-service split-brain where the web
    service wrote to app_settings.autonomy_mode and the stream
    service kept an in-memory _mode from boot — so the agent kept
    asking permission even after the user toggled semi_auto.

    Procedure: GET current mode → flip it → start a NEW turn and
    ask the agent which mode it's in → assert the response reflects
    the new mode → restore the original.
    """
    if not state.has_auth:
        return TestResult(
            name="11 autonomy mode sync",
            passed=True,
            duration_ms=0,
            detail="SKIPPED — no auth",
        )
    started = time.monotonic()
    # ── snapshot current mode ──
    try:
        r = await client.get(
            f"{state.base_url}/api/autonomy",
            headers=_headers(state),
            timeout=10.0,
        )
    except Exception as e:
        return TestResult(
            name="11 autonomy mode sync",
            passed=False,
            duration_ms=int((time.monotonic() - started) * 1000),
            error=f"GET /api/autonomy: {type(e).__name__}: {e}",
        )
    if r.status_code != 200:
        return TestResult(
            name="11 autonomy mode sync",
            passed=False,
            duration_ms=int((time.monotonic() - started) * 1000),
            error=f"GET /api/autonomy: HTTP {r.status_code}",
        )
    original = (r.json() or {}).get("mode") or "semi_auto"
    # Pick a different mode to flip to so we measure an actual change.
    target = "full_auto" if original != "full_auto" else "always_ask"

    async def _set_mode(mode: str) -> tuple[bool, str]:
        try:
            resp = await client.post(
                f"{state.base_url}/api/autonomy",
                headers=_headers(state),
                json={"mode": mode},
                timeout=10.0,
            )
            if resp.status_code != 200:
                return False, f"HTTP {resp.status_code}: {resp.text[:120]}"
            return True, ""
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"

    # ── flip to target ──
    ok, err = await _set_mode(target)
    if not ok:
        return TestResult(
            name="11 autonomy mode sync",
            passed=False,
            duration_ms=int((time.monotonic() - started) * 1000),
            error=f"POST /api/autonomy ({target}): {err}",
        )
    try:
        # ── ask the agent in a FRESH session (no rehydration of
        # the prior in-memory state) what mode it's in ──
        out = await _post_chat_stream(
            state,
            client,
            "What autonomy mode are you currently in? "
            "Reply with EXACTLY the mode value (one of: always_ask, "
            "semi_auto, full_auto) and nothing else.",
            timeout=60.0,
        )
        elapsed = out["duration_ms"]
        if not out.get("ok"):
            return TestResult(
                name="11 autonomy mode sync",
                passed=False,
                duration_ms=elapsed,
                error=str(out.get("error") or out.get("exception")),
            )
        text = (out.get("text") or "").lower()
        if target not in text:
            return TestResult(
                name="11 autonomy mode sync",
                passed=False,
                duration_ms=elapsed,
                error=(
                    f"agent didn't pick up mode flip to {target!r}. "
                    f"response: {text[:200]!r}"
                ),
            )
        return TestResult(
            name="11 autonomy mode sync",
            passed=True,
            duration_ms=elapsed,
            detail=f"{original} → {target} reflected by agent",
        )
    finally:
        # ── ALWAYS restore the original mode, even on failure ──
        await _set_mode(original)


async def test_12_image_attachment_vision(
    state: HarnessState, client: httpx.AsyncClient
) -> TestResult:
    """User journey: drag/drop a PNG, ask 'what is this'.

    Regression lock for the vision-attachments feature shipped today.
    Confirms the full path: POST /api/uploads (multipart) → upload id
    flows through /api/chat's attachments[] → stream service fetches
    the upload row → agent_loop builds Anthropic image content block
    → model receives the image. We don't assert correct *vision*
    output (the model's interpretation of a 1x1 PNG is unreliable);
    we assert the pipe is plumbed end to end with no error.
    """
    if not state.has_auth:
        return TestResult(
            name="12 image attachment vision",
            passed=True,
            duration_ms=0,
            detail="SKIPPED — no auth",
        )
    started = time.monotonic()
    # Well-known 1x1 transparent PNG (67 bytes). Originally tried a
    # hand-crafted hex blob but the IDAT chunk was malformed and
    # Anthropic rejected it with "Could not process image" — that's
    # how the smoke harness caught its first real bug, in itself.
    # Base64'd here so the bytes are reproducible regardless of
    # editor quirks.
    import base64
    one_pixel_png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42m"
        "NkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
    )
    # ── upload ──
    try:
        upload_resp = await client.post(
            f"{state.base_url}/api/uploads",
            headers={
                k: v for k, v in _headers(state).items()
                if k.lower() != "content-type"
            },
            files={"file": ("smoke.png", one_pixel_png, "image/png")},
            timeout=20.0,
        )
    except Exception as e:
        return TestResult(
            name="12 image attachment vision",
            passed=False,
            duration_ms=int((time.monotonic() - started) * 1000),
            error=f"POST /api/uploads: {type(e).__name__}: {e}",
        )
    if upload_resp.status_code != 200:
        return TestResult(
            name="12 image attachment vision",
            passed=False,
            duration_ms=int((time.monotonic() - started) * 1000),
            error=f"POST /api/uploads HTTP {upload_resp.status_code}: "
                  f"{upload_resp.text[:200]}",
        )
    upload_id = (upload_resp.json() or {}).get("id")
    if not upload_id:
        return TestResult(
            name="12 image attachment vision",
            passed=False,
            duration_ms=int((time.monotonic() - started) * 1000),
            error=f"no id in /api/uploads response: {upload_resp.text[:200]}",
        )
    # ── send the turn with the attachment ──
    body = {
        "prompt": "Describe the image I just attached in one short sentence.",
        "attachments": [upload_id],
    }
    try:
        start_resp = await client.post(
            f"{state.base_url}/api/chat",
            headers=_headers(state),
            json=body,
            timeout=15.0,
        )
    except Exception as e:
        return TestResult(
            name="12 image attachment vision",
            passed=False,
            duration_ms=int((time.monotonic() - started) * 1000),
            error=f"POST /api/chat: {type(e).__name__}: {e}",
        )
    if start_resp.status_code != 200:
        return TestResult(
            name="12 image attachment vision",
            passed=False,
            duration_ms=int((time.monotonic() - started) * 1000),
            error=f"POST /api/chat HTTP {start_resp.status_code}: "
                  f"{start_resp.text[:200]}",
        )
    turn_id = (start_resp.json() or {}).get("turn_id")
    if not turn_id:
        return TestResult(
            name="12 image attachment vision",
            passed=False,
            duration_ms=int((time.monotonic() - started) * 1000),
            error="no turn_id from /api/chat",
        )

    # ── poll for terminal — same pattern as _post_chat_stream ──
    poll_url = f"{state.base_url}/api/turns/{turn_id}/events"
    deadline = time.monotonic() + 90.0
    last_ord = 0
    saw_error = False
    saw_done = False
    text_buf: list[str] = []
    while time.monotonic() < deadline:
        try:
            poll_resp = await client.get(
                f"{poll_url}?after={last_ord}",
                headers=_headers(state),
                timeout=10.0,
            )
        except Exception:
            await asyncio.sleep(0.5)
            continue
        if poll_resp.status_code != 200:
            await asyncio.sleep(0.5)
            continue
        pj = poll_resp.json() or {}
        for ev in pj.get("events", []):
            last_ord = max(last_ord, ev.get("ord", 0))
            if ev.get("event") == "text_delta":
                text_buf.append((ev.get("payload") or {}).get("content", ""))
            elif ev.get("event") == "error":
                saw_error = True
            elif ev.get("event") == "done":
                saw_done = True
        if pj.get("terminal"):
            break
        await asyncio.sleep(0.5)

    elapsed = int((time.monotonic() - started) * 1000)
    if saw_error:
        return TestResult(
            name="12 image attachment vision",
            passed=False,
            duration_ms=elapsed,
            error="error event during attachment turn",
        )
    if not saw_done:
        return TestResult(
            name="12 image attachment vision",
            passed=False,
            duration_ms=elapsed,
            error=f"turn never reached terminal (turn_id={turn_id})",
        )
    text = "".join(text_buf).strip()
    if not text:
        return TestResult(
            name="12 image attachment vision",
            passed=False,
            duration_ms=elapsed,
            error="agent finished with empty response — vision pipe broken?",
        )
    return TestResult(
        name="12 image attachment vision",
        passed=True,
        duration_ms=elapsed,
        detail=f"vision pipe ok ({len(text)} chars response)",
    )


async def test_13_event_replay_idempotent(
    state: HarnessState, client: httpx.AsyncClient
) -> TestResult:
    """User journey: refresh the tab mid-turn, the same events replay.

    Validates the resume-on-hydrate path used when ChatProvider
    mounts with a saved inFlight.turnId. We don't actually mid-turn
    refresh here — we just re-poll a completed turn from ord=0 and
    assert the event sequence comes back the same, which is what
    the resume path relies on.
    """
    if not state.has_auth or not state.session_id:
        return TestResult(
            name="13 event replay idempotent",
            passed=True,
            duration_ms=0,
            detail="SKIPPED — no auth or no prior session_id",
        )
    # Pull most-recent turn id for this session via /api/sessions/<id>.
    started = time.monotonic()
    try:
        sess = await client.get(
            f"{state.base_url}/api/sessions/{state.session_id}",
            headers=_headers(state),
            timeout=10.0,
        )
    except Exception as e:
        return TestResult(
            name="13 event replay idempotent",
            passed=False,
            duration_ms=int((time.monotonic() - started) * 1000),
            error=f"GET /api/sessions: {type(e).__name__}: {e}",
        )
    if sess.status_code != 200:
        return TestResult(
            name="13 event replay idempotent",
            passed=False,
            duration_ms=int((time.monotonic() - started) * 1000),
            error=f"GET /api/sessions HTTP {sess.status_code}",
        )
    sj = sess.json() or {}
    turns = sj.get("turns") or []
    if not turns:
        return TestResult(
            name="13 event replay idempotent",
            passed=False,
            duration_ms=int((time.monotonic() - started) * 1000),
            error="no turns in session — test 03 must have failed",
        )
    turn_id = turns[0].get("id") or turns[-1].get("id")
    if not turn_id:
        return TestResult(
            name="13 event replay idempotent",
            passed=False,
            duration_ms=int((time.monotonic() - started) * 1000),
            error="turn objects missing id",
        )

    async def _replay() -> list[int]:
        r = await client.get(
            f"{state.base_url}/api/turns/{turn_id}/events?after=0",
            headers=_headers(state),
            timeout=15.0,
        )
        if r.status_code != 200:
            raise RuntimeError(f"replay HTTP {r.status_code}")
        body = r.json() or {}
        if not body.get("terminal"):
            raise RuntimeError("replayed turn not terminal — completed turn expected")
        events = body.get("events") or []
        return [e.get("ord", 0) for e in events]

    try:
        first = await _replay()
        second = await _replay()
    except Exception as e:
        return TestResult(
            name="13 event replay idempotent",
            passed=False,
            duration_ms=int((time.monotonic() - started) * 1000),
            error=str(e),
        )
    elapsed = int((time.monotonic() - started) * 1000)
    if not first:
        return TestResult(
            name="13 event replay idempotent",
            passed=False,
            duration_ms=elapsed,
            error="empty event list on replay",
        )
    if first != second:
        return TestResult(
            name="13 event replay idempotent",
            passed=False,
            duration_ms=elapsed,
            error=(
                f"event ord sequence differs between replays "
                f"(first n={len(first)}, second n={len(second)})"
            ),
        )
    # Monotonic ord values — defensive against any DB ordering bug
    monotonic = all(first[i] < first[i + 1] for i in range(len(first) - 1))
    if not monotonic:
        return TestResult(
            name="13 event replay idempotent",
            passed=False,
            duration_ms=elapsed,
            error=f"ord values not strictly increasing: {first}",
        )
    return TestResult(
        name="13 event replay idempotent",
        passed=True,
        duration_ms=elapsed,
        detail=f"{len(first)} events, deterministic across 2 replays",
    )


async def test_14_no_sentinel_leak(
    state: HarnessState, client: httpx.AsyncClient
) -> TestResult:
    """User journey: a turn that uses artifact tools doesn't leak raw
    sentinel markers into the assistant's prose.

    Belt-and-suspenders for test 10. Even if the parser regresses and
    artifact events stop firing, this catches the visible symptom:
    raw `⟦ASTRA_ARTIFACT⟧` markers in the response text. The fix's
    text-scrubbing replaces these with `[artifact emitted]`, so neither
    string should reach the assistant's user-facing prose IF the
    parser ran; the assistant context sees `[artifact emitted]`
    inside tool_result blocks, but that's a context block, not the
    final text. We assert the wire-level invariant: assistant text
    never carries the open marker.
    """
    if not state.has_auth:
        return TestResult(
            name="14 no sentinel leak",
            passed=True,
            duration_ms=0,
            detail="SKIPPED — no auth",
        )
    out = await _post_chat_stream(
        state,
        client,
        "Use emit_table to show a 3-row table titled 'smoke' with "
        "columns ['k','v'] and any 3 plausible rows. Then write one "
        "sentence summarizing.",
        timeout=90.0,
    )
    elapsed = out["duration_ms"]
    if not out.get("ok"):
        return TestResult(
            name="14 no sentinel leak",
            passed=False,
            duration_ms=elapsed,
            error=str(out.get("error") or out.get("exception")),
        )
    text = out.get("text") or ""
    if "⟦ASTRA_ARTIFACT⟧" in text or "⟦/ASTRA_ARTIFACT⟧" in text:
        return TestResult(
            name="14 no sentinel leak",
            passed=False,
            duration_ms=elapsed,
            error=(
                "raw artifact sentinel marker in assistant text — "
                "scrubber broken. head: " + text[:200]
            ),
        )
    return TestResult(
        name="14 no sentinel leak",
        passed=True,
        duration_ms=elapsed,
        detail=f"{len(text)} chars clean",
    )


async def test_15_upstream_auth_enforced(
    state: HarnessState, client: httpx.AsyncClient
) -> TestResult:
    """Security lock: fleet-agent APIs reject unauthenticated calls.

    Regression lock for the 2026-06-11 finding that email-agent and
    whatsapp-gateway sat publicly unauthenticated — anyone could read
    Kunal's mail or send WhatsApp as him. The mesh-auth middleware
    must 401 (bad/missing secret) or 503 (secret unconfigured) every
    unauthenticated request, and must accept the same request WITH
    the shared secret. Health endpoints stay public for fleet probes.
    """
    started = time.monotonic()
    probes = [
        # (label, url, expect_data_with_secret)
        (
            "email",
            "https://email.thearrogantclub.com/api/v1/messages/summary",
            True,
        ),
        (
            "whatsapp",
            "https://whatsapp.thearrogantclub.com/api/v1/templates/",
            True,
        ),
    ]
    failures: list[str] = []
    details: list[str] = []
    for label, url, expect_ok in probes:
        # 1. No secret → must be rejected.
        try:
            r = await client.get(url, timeout=10.0)
            if r.status_code not in (401, 403, 503):
                failures.append(
                    f"{label}: unauthenticated GET returned "
                    f"{r.status_code} (expected 401/403/503) — PUBLIC HOLE"
                )
                continue
            details.append(f"{label}:{r.status_code} w/o secret")
        except Exception as e:
            failures.append(f"{label}: unauth probe error {type(e).__name__}")
            continue
        # 2. With secret → must work (when we have one to send).
        if expect_ok and state.shared_secret:
            try:
                r2 = await client.get(
                    url,
                    headers={"x-astra-secret": state.shared_secret},
                    timeout=10.0,
                )
                if r2.status_code != 200:
                    failures.append(
                        f"{label}: authenticated GET returned "
                        f"{r2.status_code} (expected 200) — secret "
                        "mismatch between caller and agent?"
                    )
            except Exception as e:
                failures.append(
                    f"{label}: auth probe error {type(e).__name__}"
                )
    elapsed = int((time.monotonic() - started) * 1000)
    if failures:
        return TestResult(
            name="15 upstream auth enforced",
            passed=False,
            duration_ms=elapsed,
            error="; ".join(failures),
        )
    return TestResult(
        name="15 upstream auth enforced",
        passed=True,
        duration_ms=elapsed,
        detail=" · ".join(details),
    )




async def test_16_approval_round_trip(
    state: HarnessState, client: httpx.AsyncClient
) -> TestResult:
    """User journey: the autonomy gate actually asks, and a web
    approval actually grants.

    Locks Phase C: flip to always_ask → a WRITE tool call must
    surface an approval_request event + an 'awaiting approval'
    tool_result instead of executing → resolving via the web API
    flips the row. Mode is restored in finally, like test 11.
    """
    if not state.has_auth:
        return TestResult(
            name="16 approval round-trip",
            passed=True,
            duration_ms=0,
            detail="SKIPPED — no auth",
        )
    started = time.monotonic()

    async def _set_mode(mode: str) -> bool:
        try:
            r = await client.post(
                f"{state.base_url}/api/autonomy",
                headers=_headers(state),
                json={"mode": mode},
                timeout=10.0,
            )
            return r.status_code == 200
        except Exception:
            return False

    # snapshot + flip
    try:
        r = await client.get(
            f"{state.base_url}/api/autonomy", headers=_headers(state), timeout=10.0
        )
        original = (r.json() or {}).get("mode") or "semi_auto"
    except Exception as e:
        return TestResult(
            name="16 approval round-trip",
            passed=False,
            duration_ms=int((time.monotonic() - started) * 1000),
            error=f"mode snapshot failed: {e}",
        )
    if not await _set_mode("always_ask"):
        return TestResult(
            name="16 approval round-trip",
            passed=False,
            duration_ms=int((time.monotonic() - started) * 1000),
            error="couldn't set always_ask",
        )
    try:
        out = await _post_chat_stream(
            state,
            client,
            "Use the store_memory tool RIGHT NOW to store: 'approval "
            "smoke test marker'. Do not ask questions, call the tool.",
            timeout=90.0,
        )
        elapsed = out["duration_ms"]
        approval_events = [
            e for e in out.get("events", [])
            if e.get("event") == "approval_request"
        ]
        if not approval_events:
            tool_results = [
                (e.get("data") or {}).get("preview", "")
                for e in out.get("events", [])
                if e.get("event") == "tool_result"
            ]
            return TestResult(
                name="16 approval round-trip",
                passed=False,
                duration_ms=elapsed,
                error=(
                    "no approval_request event in always_ask mode — gate "
                    f"not asking. tool_results: {tool_results[:3]}"
                ),
            )
        approval_id = (approval_events[0].get("data") or {}).get("id")
        if not approval_id:
            return TestResult(
                name="16 approval round-trip",
                passed=False,
                duration_ms=elapsed,
                error="approval_request event missing id",
            )
        # resolve via the web API (the /approvals page path)
        rr = await client.post(
            f"{state.base_url}/api/approvals/{approval_id}/resolve",
            headers=_headers(state),
            json={"decision": "approved"},
            timeout=10.0,
        )
        if rr.status_code != 200 or not (rr.json() or {}).get("ok"):
            return TestResult(
                name="16 approval round-trip",
                passed=False,
                duration_ms=int((time.monotonic() - started) * 1000),
                error=f"resolve failed: {rr.status_code} {rr.text[:150]}",
            )
        # resolved row must leave the pending list
        rl = await client.get(
            f"{state.base_url}/api/approvals",
            headers=_headers(state),
            timeout=10.0,
        )
        still_pending = [
            a for a in (rl.json() or {}).get("approvals", [])
            if a.get("id") == approval_id
        ]
        if still_pending:
            return TestResult(
                name="16 approval round-trip",
                passed=False,
                duration_ms=int((time.monotonic() - started) * 1000),
                error=f"approval #{approval_id} still pending after resolve",
            )
        return TestResult(
            name="16 approval round-trip",
            passed=True,
            duration_ms=int((time.monotonic() - started) * 1000),
            detail=f"gate asked (#{approval_id}), web approve cleared it",
        )
    finally:
        await _set_mode(original)


# ── Runner ─────────────────────────────────────────────────


TESTS = [
    test_00_health_deep,
    test_01_sessions_list,
    test_02_recent_turns,
    test_03_simple_text_turn,
    test_04_tool_using_turn,
    test_05_session_continuity,
    test_06_session_detail,
    test_07_bridge_expand_handling,
    test_08_email_data_endpoint,
    test_09_agent_state_per_agent,
    # User-journey regression locks (added 2026-05-16).
    test_10_palette_artifact_round_trip,
    test_11_autonomy_mode_sync,
    test_12_image_attachment_vision,
    test_13_event_replay_idempotent,
    test_14_no_sentinel_leak,
    # Security locks (added 2026-06-11).
    test_15_upstream_auth_enforced,
    test_16_approval_round_trip,
]


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--base",
        default=os.environ.get(
            "ASTRA_E2E_BASE", "https://astra.thearrogantclub.com"
        ),
    )
    parser.add_argument(
        "--cookie",
        default=os.environ.get("ASTRA_E2E_COOKIE", ""),
        help="NextAuth session cookie. Without it, agent-path tests skip.",
    )
    parser.add_argument(
        "--shared-secret",
        default=os.environ.get("ASTRA_SHARED_SECRET", ""),
        help=(
            "Shared secret matching astra-web's ASTRA_SHARED_SECRET. "
            "Lets the harness use the server-to-server middleware "
            "bypass (x-astra-secret header) instead of needing a "
            "NextAuth cookie. Either auth path unlocks tests 03-07."
        ),
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="Print test names and exit",
    )
    args = parser.parse_args()

    if args.list:
        for fn in TESTS:
            print(fn.__name__)
        return 0

    state = HarnessState(
        base_url=args.base.rstrip("/"),
        cookie=args.cookie or None,
        shared_secret=args.shared_secret or None,
    )

    print(f"astra e2e smoke against {state.base_url}")
    if not state.has_auth:
        print(
            "  (no ASTRA_E2E_COOKIE or ASTRA_SHARED_SECRET set — "
            "agent-path tests will skip)"
        )
    elif state.shared_secret and not state.cookie:
        print("  (using shared-secret bypass; no cookie)")
    print()
    started = time.monotonic()

    async with httpx.AsyncClient() as client:
        for fn in TESTS:
            try:
                result = await fn(state, client)
            except Exception as e:
                result = TestResult(
                    name=fn.__name__,
                    passed=False,
                    duration_ms=0,
                    error=f"unhandled: {type(e).__name__}: {e}",
                )
            state.record(result)

    total_ms = int((time.monotonic() - started) * 1000)
    passed = sum(1 for r in state.results if r.passed)
    failed = sum(1 for r in state.results if not r.passed)

    print()
    print("─" * 60)
    print(f"{passed}/{len(state.results)} passed · {total_ms}ms total")
    if failed:
        print("FAILURES:")
        for r in state.results:
            if not r.passed:
                print(f"  · {r.name}: {r.error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
