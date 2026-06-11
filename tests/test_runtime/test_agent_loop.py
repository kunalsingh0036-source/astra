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


class _FakeBlock:
    """Stand-in for anthropic SDK content blocks. Mirrors the shape
    the agent loop reads (`type`, `text`, `id`, `name`, `input`)
    without pulling in the SDK's actual block classes.

    Defined here at module top so all fakes (Phase 2 + Phase 3) can
    use it without circular import gymnastics.
    """

    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


class _FakeFinalMessage:
    """Stand-in for the message returned by stream.get_final_message().
    Only exposes the two attributes the agent loop reads."""

    def __init__(self, *, stop_reason: str, content: list) -> None:
        self.stop_reason = stop_reason
        self.content = content


class _FakeStream:
    """Stand-in for `anthropic.AsyncMessageStream` async-context-manager.

    Yields a fixed list of text chunks via `text_stream`, then exits
    with stop_reason='end_turn' when get_final_message() is called.
    Mirrors the public surface the agent loop touches — does not
    pretend to be the full anthropic SDK.
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

    async def get_final_message(self) -> "_FakeFinalMessage":
        # Phase 2 fakes always end the turn. The full text stream
        # becomes one text content block; no tools.
        return _FakeFinalMessage(
            stop_reason="end_turn",
            content=[
                _FakeBlock(type="text", text="".join(self._chunks)),
            ],
        )


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
        async for f in run_lean_turn(
            "hi", session_id="s", tools_enabled=False
        ):
            name, data = _parse_sse_frame(f)
            if name == "text_delta":
                text_frames.append(data["content"])

    # Should be sliced into multiple smaller frames
    assert len(text_frames) >= 2
    # And the concatenation should equal the original
    assert "".join(text_frames) == long_chunk
    # No single frame should exceed the slice cap
    assert all(len(t) <= 4096 for t in text_frames)


# ── Phase 3: Tool dispatch ──────────────────────────────────


class _FakeStreamWithTools:
    """Like _FakeStream but supports tool_use stop_reason. Each
    invocation pops the next 'turn' from a queue, so a single test
    can simulate multi-iteration loops (text → tool_use → text)."""

    def __init__(self, queue: list[dict]) -> None:
        self._queue = queue
        self._current = None

    async def __aenter__(self) -> "_FakeStreamWithTools":
        # Pull the next planned turn from the queue
        self._current = self._queue.pop(0) if self._queue else {
            "text_chunks": ["(end)"],
            "stop_reason": "end_turn",
            "content": [_FakeBlock(type="text", text="(end)")],
        }
        return self

    async def __aexit__(self, *args) -> None:
        return None

    @property
    def text_stream(self) -> AsyncIterator[str]:
        chunks = self._current.get("text_chunks", []) if self._current else []

        async def gen() -> AsyncIterator[str]:
            for c in chunks:
                await asyncio.sleep(0)
                yield c

        return gen()

    async def get_final_message(self) -> _FakeFinalMessage:
        return _FakeFinalMessage(
            stop_reason=self._current["stop_reason"],
            content=self._current["content"],
        )


class _FakeClientWithTools:
    def __init__(self, turn_queue: list[dict]) -> None:
        self._queue = turn_queue
        self.messages = self

    def stream(self, **_kwargs):
        return _FakeStreamWithTools(self._queue)


@pytest.mark.asyncio
async def test_tool_dispatch_full_loop(monkeypatch) -> None:
    """End-to-end: model returns tool_use → loop dispatches via
    REGISTRY → tool_result event emitted → conversation continues
    with the tool result → model returns end_turn text."""
    from astra.runtime.tool_registry import (
        ActionTier,
        ToolDef,
        ToolRegistry,
    )

    # Use a fresh registry so global state isn't polluted.
    test_registry = ToolRegistry()

    async def double(args: dict) -> str:
        n = int(args.get("n", 0))
        return f"doubled: {n * 2}"

    test_registry.register(
        ToolDef(
            name="double",
            description="Double a number",
            input_schema={
                "type": "object",
                "properties": {"n": {"type": "integer"}},
            },
            fn=double,
            tier=ActionTier.READ,
        )
    )

    # Patch the global REGISTRY the agent loop reads.
    monkeypatch.setattr("astra.runtime.agent_loop.REGISTRY", test_registry)

    # Iteration 1: model wants to call `double` with n=21
    # Iteration 2: model returns final text ("the answer is 42")
    queue = [
        {
            "text_chunks": ["I'll compute that. "],
            "stop_reason": "tool_use",
            "content": [
                _FakeBlock(type="text", text="I'll compute that. "),
                _FakeBlock(
                    type="tool_use",
                    id="tu_001",
                    name="double",
                    input={"n": 21},
                ),
            ],
        },
        {
            "text_chunks": ["The answer is 42."],
            "stop_reason": "end_turn",
            "content": [_FakeBlock(type="text", text="The answer is 42.")],
        },
    ]
    fake = _FakeClientWithTools(queue)

    with patch("astra.runtime.agent_loop.AsyncAnthropic", return_value=fake):
        frames = []
        async for f in run_lean_turn("multiply 21 by 2", session_id="s"):
            frames.append(_parse_sse_frame(f))

    names = [n for n, _ in frames]
    # Must see: session, then text_delta(s), tool_call, tool_result,
    # more text_delta(s), done.
    assert names[0] == "session"
    assert "tool_call" in names
    assert "tool_result" in names
    assert names[-1] == "done"

    tc = next(p for n, p in frames if n == "tool_call")
    assert tc["id"] == "tu_001"
    assert tc["name"] == "double"

    tr = next(p for n, p in frames if n == "tool_result")
    assert tr["id"] == "tu_001"
    assert "doubled: 42" in tr["preview"]
    assert tr["is_error"] is False

    # Final text should appear in the deltas
    text = "".join(p["content"] for n, p in frames if n == "text_delta")
    assert "I'll compute that." in text
    assert "The answer is 42." in text

    # done event includes tool_count
    done_payload = frames[-1][1]
    assert done_payload.get("tool_count") == 1


@pytest.mark.asyncio
async def test_unknown_tool_surfaces_as_tool_result_error(monkeypatch) -> None:
    """If the model hallucinates a tool name not in the registry, we
    must NOT crash — surface as tool_result with is_error=True so the
    model can recover."""
    from astra.runtime.tool_registry import ToolRegistry

    monkeypatch.setattr(
        "astra.runtime.agent_loop.REGISTRY", ToolRegistry()
    )

    queue = [
        {
            "text_chunks": [""],
            "stop_reason": "tool_use",
            "content": [
                _FakeBlock(
                    type="tool_use",
                    id="tu_404",
                    name="not_a_real_tool",
                    input={},
                ),
            ],
        },
        {
            "text_chunks": ["Sorry, that tool doesn't exist."],
            "stop_reason": "end_turn",
            "content": [
                _FakeBlock(type="text", text="Sorry, that tool doesn't exist.")
            ],
        },
    ]
    fake = _FakeClientWithTools(queue)

    with patch("astra.runtime.agent_loop.AsyncAnthropic", return_value=fake):
        frames = []
        async for f in run_lean_turn("use not_a_real_tool", session_id="s"):
            frames.append(_parse_sse_frame(f))

    # Tool result should be present with is_error=True
    tr = next(p for n, p in frames if n == "tool_result")
    assert tr["is_error"] is True
    assert "unknown tool" in tr["preview"]
    # Loop must have continued and produced the final apology text
    text = "".join(p["content"] for n, p in frames if n == "text_delta")
    assert "doesn't exist" in text


@pytest.mark.asyncio
async def test_tool_raises_becomes_tool_result_error(monkeypatch) -> None:
    """A tool that raises an exception is a programming bug. The
    registry already catches it and returns a ToolResult with
    is_error=True. The agent loop must forward that as a tool_result
    SSE event with is_error=True (NOT crash the turn)."""
    from astra.runtime.tool_registry import (
        ActionTier,
        ToolDef,
        ToolRegistry,
    )

    test_registry = ToolRegistry()

    async def boom(args: dict) -> str:
        raise ValueError("oops")

    test_registry.register(
        ToolDef(
            name="boom",
            description="explodes",
            input_schema={"type": "object"},
            fn=boom,
            tier=ActionTier.READ,
        )
    )
    monkeypatch.setattr(
        "astra.runtime.agent_loop.REGISTRY", test_registry
    )

    queue = [
        {
            "text_chunks": [""],
            "stop_reason": "tool_use",
            "content": [
                _FakeBlock(
                    type="tool_use",
                    id="tu_b",
                    name="boom",
                    input={},
                ),
            ],
        },
        {
            "text_chunks": ["The tool failed."],
            "stop_reason": "end_turn",
            "content": [_FakeBlock(type="text", text="The tool failed.")],
        },
    ]
    fake = _FakeClientWithTools(queue)

    with patch("astra.runtime.agent_loop.AsyncAnthropic", return_value=fake):
        frames = []
        async for f in run_lean_turn("hi", session_id="s"):
            frames.append(_parse_sse_frame(f))

    tr = next(p for n, p in frames if n == "tool_result")
    assert tr["is_error"] is True
    assert "ValueError" in tr["preview"] or "oops" in tr["preview"]
    # Conversation continued — the agent loop didn't crash
    assert frames[-1][0] == "done"


@pytest.mark.asyncio
async def test_tools_disabled_skips_tool_dispatch(monkeypatch) -> None:
    """tools_enabled=False makes the loop run in pure-text mode (Phase
    2 behavior). Useful for flows where tools don't make sense.
    Verifies that even if the registry has tools, none are exposed."""
    from astra.runtime.tool_registry import (
        ActionTier,
        ToolDef,
        ToolRegistry,
    )

    test_registry = ToolRegistry()

    async def fn(args: dict) -> str:
        return "should not be called"

    test_registry.register(
        ToolDef(
            name="never",
            description="",
            input_schema={"type": "object"},
            fn=fn,
            tier=ActionTier.READ,
        )
    )
    monkeypatch.setattr(
        "astra.runtime.agent_loop.REGISTRY", test_registry
    )

    fake = _FakeClient(["just text"])
    with patch("astra.runtime.agent_loop.AsyncAnthropic", return_value=fake):
        frames = []
        async for f in run_lean_turn(
            "hi", session_id="s", tools_enabled=False
        ):
            frames.append(_parse_sse_frame(f))

    # No tool_call events should ever fire
    assert all(n != "tool_call" for n, _ in frames)
    assert frames[-1][0] == "done"


# ── Artifact sentinel parsing ──────────────────────────────


def test_extract_artifacts_no_sentinel() -> None:
    """Plain tool-result text is returned untouched."""
    from astra.runtime.agent_loop import _extract_artifacts

    found, scrubbed = _extract_artifacts("ordinary tool output, no markers")
    assert found == []
    assert scrubbed == "ordinary tool output, no markers"


def test_extract_artifacts_single_palette() -> None:
    """A single sentinel becomes one parsed payload + a placeholder."""
    from astra.runtime.agent_loop import _extract_artifacts

    body = (
        '{"type":"palette","name":"Film Noir",'
        '"colors":[{"hex":"#0A0A0A","label":"deep"}],'
        '"notes":"cinematic"}'
    )
    text = f"⟦ASTRA_ARTIFACT⟧{body}⟦/ASTRA_ARTIFACT⟧"
    found, scrubbed = _extract_artifacts(text)
    assert len(found) == 1
    assert found[0]["type"] == "palette"
    assert found[0]["name"] == "Film Noir"
    assert found[0]["colors"][0]["hex"] == "#0A0A0A"
    assert scrubbed == "[artifact emitted]"


def test_extract_artifacts_multiple_in_one_result() -> None:
    """A tool that emitted N artifacts gives N payloads, prose stays."""
    from astra.runtime.agent_loop import _extract_artifacts

    text = (
        "before "
        '⟦ASTRA_ARTIFACT⟧{"type":"palette","name":"A"}⟦/ASTRA_ARTIFACT⟧'
        " middle "
        '⟦ASTRA_ARTIFACT⟧{"type":"palette","name":"B"}⟦/ASTRA_ARTIFACT⟧'
        " after"
    )
    found, scrubbed = _extract_artifacts(text)
    assert [a["name"] for a in found] == ["A", "B"]
    assert scrubbed == "before [artifact emitted] middle [artifact emitted] after"


def test_extract_artifacts_malformed_json_passes_through() -> None:
    """A sentinel wrapping invalid JSON stays raw — better to leak the
    marker than to silently drop content the model meant to send."""
    from astra.runtime.agent_loop import _extract_artifacts

    text = "⟦ASTRA_ARTIFACT⟧{not, valid, json}⟦/ASTRA_ARTIFACT⟧"
    found, scrubbed = _extract_artifacts(text)
    assert found == []
    assert "ASTRA_ARTIFACT" in scrubbed


def test_extract_artifacts_unclosed_sentinel_passes_through() -> None:
    """An open marker with no close marker (the model never wrote one)
    is treated as raw text. We don't try to guess where it ended."""
    from astra.runtime.agent_loop import _extract_artifacts

    text = '⟦ASTRA_ARTIFACT⟧{"type":"palette"} but no close marker'
    found, scrubbed = _extract_artifacts(text)
    assert found == []
    assert "ASTRA_ARTIFACT" in scrubbed


def test_extract_artifacts_non_dict_json_passes_through() -> None:
    """Valid JSON but not a dict (e.g. a bare string or list) isn't a
    valid artifact payload — leave it raw."""
    from astra.runtime.agent_loop import _extract_artifacts

    text = '⟦ASTRA_ARTIFACT⟧"just a string"⟦/ASTRA_ARTIFACT⟧'
    found, scrubbed = _extract_artifacts(text)
    assert found == []
    assert "ASTRA_ARTIFACT" in scrubbed


# ── Empty-output guard ─────────────────────────────────────


class _EmptyStream(_FakeStream):
    """Stream that yields zero text chunks and ends with stop_reason
    and content the caller can configure. Used to simulate the
    "model finished without saying anything" failure mode."""

    def __init__(self, stop_reason: str = "end_turn") -> None:
        super().__init__([])
        self._stop_reason = stop_reason

    async def get_final_message(self) -> "_FakeFinalMessage":
        return _FakeFinalMessage(stop_reason=self._stop_reason, content=[])


class _EmptyClient:
    def __init__(self, stop_reason: str = "end_turn") -> None:
        self.messages = self
        self._stop_reason = stop_reason

    def stream(self, **_kwargs):
        return _EmptyStream(self._stop_reason)


@pytest.mark.asyncio
async def test_empty_turn_surfaces_error() -> None:
    """If the model finishes a turn with no text and no artifacts,
    the user must see an error explaining what happened — not a
    blank pane with `answered in Xs`. Real failure mode: model
    recurses through read-only tools without producing output."""
    fake = _EmptyClient(stop_reason="end_turn")
    with patch("astra.runtime.agent_loop.AsyncAnthropic", return_value=fake):
        frames = []
        async for f in run_lean_turn("hi", session_id="s", tools_enabled=False):
            frames.append(_parse_sse_frame(f))

    error_frames = [f for f in frames if f[0] == "error"]
    assert len(error_frames) >= 1, "expected an error event for empty turn"
    msg = error_frames[0][1]["message"]
    assert "without producing a response" in msg.lower() or "stuck" in msg.lower()
    # And we still get a done frame so the client transitions out of
    # the streaming state cleanly.
    assert frames[-1][0] == "done"


@pytest.mark.asyncio
async def test_max_tokens_surfaces_truncation_error() -> None:
    """stop_reason='max_tokens' isn't a clean exit — surface it as a
    truncation error so the user knows the response was cut off."""
    fake = _EmptyClient(stop_reason="max_tokens")
    with patch("astra.runtime.agent_loop.AsyncAnthropic", return_value=fake):
        frames = []
        async for f in run_lean_turn("hi", session_id="s", tools_enabled=False):
            frames.append(_parse_sse_frame(f))

    error_frames = [f for f in frames if f[0] == "error"]
    # At least one error should mention truncation
    assert any(
        "truncat" in f[1]["message"].lower()
        or "max_tokens" in f[1]["message"].lower()
        for f in error_frames
    ), f"expected a truncation error, got: {error_frames}"


@pytest.mark.asyncio
async def test_refusal_surfaces_refusal_error() -> None:
    """stop_reason='refusal' must produce a visible message; silent
    refusal feels like the agent is broken."""
    fake = _EmptyClient(stop_reason="refusal")
    with patch("astra.runtime.agent_loop.AsyncAnthropic", return_value=fake):
        frames = []
        async for f in run_lean_turn("hi", session_id="s", tools_enabled=False):
            frames.append(_parse_sse_frame(f))

    error_frames = [f for f in frames if f[0] == "error"]
    assert any(
        "refus" in f[1]["message"].lower() for f in error_frames
    ), f"expected a refusal-mentioning error, got: {error_frames}"


@pytest.mark.asyncio
async def test_text_only_turn_does_not_trigger_empty_guard() -> None:
    """When the model actually produces text, the empty-output guard
    must not fire. Defense against the guard becoming over-eager and
    spamming errors on normal turns."""
    fake = _FakeClient(["here is your answer"])
    with patch("astra.runtime.agent_loop.AsyncAnthropic", return_value=fake):
        frames = []
        async for f in run_lean_turn("hi", session_id="s", tools_enabled=False):
            frames.append(_parse_sse_frame(f))

    error_frames = [f for f in frames if f[0] == "error"]
    assert error_frames == [], (
        f"empty-output guard misfired on a non-empty turn: {error_frames}"
    )


# ── Turn hard cap + image hygiene (added 2026-06-11) ───────


@pytest.mark.asyncio
async def test_turn_hard_cap_enforced(monkeypatch) -> None:
    """_TURN_HARD_TIMEOUT_SEC existed for weeks as documentation-only —
    defined, AST-tested, never enforced. Now the loop must stop with a
    visible error when the deadline is breached. Cap monkeypatched to
    0 so the very first iteration check fires."""
    import astra.runtime.agent_loop as al

    monkeypatch.setattr(al, "_TURN_HARD_TIMEOUT_SEC", 0)
    fake = _FakeClient(["should never stream"])
    with patch("astra.runtime.agent_loop.AsyncAnthropic", return_value=fake):
        frames = []
        async for f in run_lean_turn("hi", session_id="s", tools_enabled=False):
            frames.append(_parse_sse_frame(f))

    error_frames = [f for f in frames if f[0] == "error"]
    assert any(
        "hard cap" in f[1]["message"] for f in error_frames
    ), f"expected hard-cap error, got: {[f[1] for f in error_frames]}"
    assert frames[-1][0] == "done"


def test_strip_image_blocks_replaces_base64_with_marker() -> None:
    """Persisted history must never carry base64 image blocks — they
    re-ship to the API every turn and blow up the token estimator
    (the 'Astra forgot everything after a screenshot' bug)."""
    from astra.runtime.session_store import _strip_image_blocks

    messages = [
        {"role": "user", "content": "plain text untouched"},
        {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": "A" * 1_000_000,
                    },
                },
                {"type": "text", "text": "what is this?"},
            ],
        },
    ]
    out = _strip_image_blocks(messages)
    assert out[0] == messages[0]
    blocks = out[1]["content"]
    assert all(b.get("type") != "image" for b in blocks)
    assert "image attachment" in blocks[0]["text"]
    assert blocks[1] == {"type": "text", "text": "what is this?"}
    # original input not mutated
    assert messages[1]["content"][0]["type"] == "image"


def test_estimator_counts_image_blocks_at_real_cost() -> None:
    """A 1MB base64 image must estimate at worst-case Anthropic image
    cost (~1.6k tokens), not chars//4 (~343k) — the overcount forced
    permanent pass-2 compaction on any session with a screenshot."""
    from astra.runtime.agent_loop import _estimate_tokens_for_block

    image_block = {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": "A" * 1_400_000,
        },
    }
    est = _estimate_tokens_for_block(image_block)
    assert est <= 2_000, f"image block estimated at {est} tokens"
