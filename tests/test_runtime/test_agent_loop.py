"""
Tests for the lean agent loop — Phase 2.

Validates that the loop:
  - Yields a session event first
  - Streams text_delta frames as Anthropic emits content
  - Yields a done event last
  - Surfaces exceptions as error events without raising
  - Enforces the per-turn hard timeout

Real Anthropic API calls are too slow + costly for unit tests, so we
patch the AsyncAnthropic client with a fake that yields canned text
chunks. The test verifies the integration shape — the loop's
translation from anthropic stream events to SSE event frames — not
the model's behavior.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import AsyncIterator
from unittest.mock import patch

import pytest

from astra.runtime.agent_loop import run_lean_turn


def _parse_sse_frame(frame: bytes) -> tuple[str, dict]:
    """Parse one 'event: name\ndata: {...}\n\n' frame back to (name, payload)."""
    text = frame.decode("utf-8")
    lines = [ln for ln in text.split("\n") if ln.strip()]
    name = ""
    data = {}
    for ln in lines:
        if ln.startswith("event: "):
            name = ln[len("event: "):]
        elif ln.startswith("data: "):
            data = json.loads(ln[len("data: "):])
    return name, data


class _FakeStream:
    """Stand-in for `anthropic.AsyncMessageStream` async-context-manager.

    Yields a fixed list of text chunks via `text_stream`, then exits.
    Mirrors the public surface the agent loop touches — does not pretend
    to be the full anthropic SDK.
    """

    def __init__(self, chunks: list[str]) -> None:
        self._chunks = chunks

    async def __aenter__(self) -> "_FakeStream":
        return self

    async def __aexit__(self, *args) -> None:
        return None

    @property
    def text_stream(self) -> AsyncIterator[str]:
        async def gen() -> AsyncIterator[str]:
            for c in self._chunks:
                # tiny await so cancellation can propagate
                await asyncio.sleep(0)
                yield c

        return gen()


class _FakeClient:
    def __init__(self, chunks: list[str]) -> None:
        self._chunks = chunks
        self.messages = self  # so .messages.stream(...) works

    def stream(self, **_kwargs):
        # Returns the async-context-manager directly, like the real SDK.
        return _FakeStream(self._chunks)


# ── Tests ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_emits_session_first_then_text_then_done() -> None:
    """Smoke test: session → text_delta(s) → done. Same event shape as
    the legacy SDK runner so the browser doesn't care which path
    answered."""
    chunks = ["Hello, ", "Kunal. ", "Migration ", "alive."]
    fake = _FakeClient(chunks)

    with patch("astra.runtime.agent_loop.AsyncAnthropic", return_value=fake):
        frames = []
        async for f in run_lean_turn("hi", session_id="test-session"):
            frames.append(_parse_sse_frame(f))

    # First frame must be session
    assert frames[0][0] == "session"
    assert frames[0][1]["session_id"] == "test-session"

    # Some text_deltas in the middle
    text_frames = [f for f in frames if f[0] == "text_delta"]
    assert len(text_frames) >= 1
    combined = "".join(f[1]["content"] for f in text_frames)
    assert combined == "Hello, Kunal. Migration alive."

    # Last frame is done
    assert frames[-1][0] == "done"
    assert "duration_ms" in frames[-1][1]


@pytest.mark.asyncio
async def test_session_minted_when_not_provided() -> None:
    """If the caller doesn't pass a session_id, the loop generates one
    and emits it on the session event so the browser knows the
    canonical id for follow-up turns."""
    fake = _FakeClient(["ok"])
    with patch("astra.runtime.agent_loop.AsyncAnthropic", return_value=fake):
        frames = []
        async for f in run_lean_turn("hi"):
            frames.append(_parse_sse_frame(f))

    assert frames[0][0] == "session"
    sid = frames[0][1]["session_id"]
    assert isinstance(sid, str)
    assert len(sid) > 0


@pytest.mark.asyncio
async def test_anthropic_exception_becomes_error_event() -> None:
    """A raising stream becomes an `error` SSE event — the loop must
    NOT re-raise. The browser stays in a clean state and shows the
    error to the user. This is the bug class we're escaping from
    (SDK CLI crashes that left the runner hung silently)."""

    class _ExplodingClient:
        messages = property(lambda self: self)  # type: ignore

        def stream(self, **_kwargs):
            class _Boom:
                async def __aenter__(self_inner):
                    raise RuntimeError("anthropic api 500")

                async def __aexit__(self_inner, *a):
                    return None

            return _Boom()

    with patch(
        "astra.runtime.agent_loop.AsyncAnthropic",
        return_value=_ExplodingClient(),
    ):
        frames = []
        async for f in run_lean_turn("hi", session_id="s"):
            frames.append(_parse_sse_frame(f))

    names = [n for n, _ in frames]
    assert "session" in names
    assert "error" in names
    assert "done" in names
    err_payload = next(p for n, p in frames if n == "error")
    assert "anthropic api 500" in err_payload["message"]


@pytest.mark.asyncio
async def test_long_chunk_is_sliced() -> None:
    """If the API ever bundles a multi-KB chunk, the loop slices it
    so individual SSE frames don't block the pipe. Verifies the
    slicing logic; in practice anthropic emits small chunks."""
    long_chunk = "x" * 10_000  # 10KB single chunk
    fake = _FakeClient([long_chunk])

    with patch("astra.runtime.agent_loop.AsyncAnthropic", return_value=fake):
        text_frames = []
        async for f in run_lean_turn("hi", session_id="s"):
            name, data = _parse_sse_frame(f)
            if name == "text_delta":
                text_frames.append(data["content"])

    # Should be sliced into multiple smaller frames
    assert len(text_frames) >= 2
    # And the concatenation should equal the original
    assert "".join(text_frames) == long_chunk
    # No single frame should exceed the slice cap
    assert all(len(t) <= 4096 for t in text_frames)
