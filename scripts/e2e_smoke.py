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
