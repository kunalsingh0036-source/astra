"""
Lean Astra agent loop — direct anthropic.AsyncAnthropic, no SDK CLI.

Phase 2 of the runtime migration: text-only streaming. No tool dispatch
yet (Phase 3 adds that). No session persistence (Phase 4). The point of
this stage is to prove that we can stream tokens straight from the
Anthropic Messages API into our existing SSE event format, with all
the reliability properties the bundled CLI subprocess lacked:

  - No subprocess (pure async Python; no opaque crashes)
  - No bundled CLI (no "Stream closed" errors from inside cli.js)
  - Per-call timeout we control (asyncio.wait_for around the stream)
  - Failures surface as real exceptions we can introspect

Once Phase 3 lands, this same loop handles tool_use stop reasons by
dispatching through ToolRegistry.dispatch() and looping again until
end_turn. For now it just streams text.

The yielded bytes match services.stream.events exactly so the browser
sees the identical SSE shape on /stream-lean as it does on /stream.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import AsyncIterator

from anthropic import AsyncAnthropic

from astra.runtime.event_emitter import (
    done,
    error,
    session as session_event,
    text_delta,
)

logger = logging.getLogger(__name__)


# Hard ceiling per turn. Real chat turns finish in 5-30s; anything over
# this is almost certainly a network problem, not the agent doing
# legitimate slow work. Phase 3+ will use per-tool timeouts on top of
# this.
_TURN_HARD_TIMEOUT_SEC = 180

# Chunk size at which we slice very long text deltas. Anthropic emits
# small deltas naturally so this rarely fires; it's defensive against
# the API ever bundling a multi-KB chunk that would block the SSE pipe.
_MAX_DELTA_CHARS = 4096


async def run_lean_turn(
    prompt: str,
    *,
    session_id: str | None = None,
    system_prompt: str = "",
    model: str = "claude-sonnet-4-5",
    max_tokens: int = 4096,
) -> AsyncIterator[bytes]:
    """Run one user turn against the Anthropic Messages API and yield
    SSE-formatted byte frames.

    Phase 2: text-only. No tools, no multi-turn message history (the
    `prompt` is sent as the only user message). Phase 3 adds tool_use
    handling; Phase 4 loads + saves session messages from Postgres.

    Yields the same SSE event shapes as services.stream.runner.run_query
    so the browser-side ChatProvider doesn't need to know which runtime
    answered.

    Args:
        prompt: the user's message
        session_id: cosmetic for now — surfaced to the browser as the
            current session id, but no message history is loaded.
        system_prompt: the Astra system prompt. Empty default lets
            callers test without the full prompt overhead.
        model: Anthropic model alias. Defaults to Sonnet.
        max_tokens: response cap.
    """
    started = time.monotonic()
    sid = session_id or str(uuid.uuid4())
    yield session_event(sid)

    client = AsyncAnthropic()

    try:
        # The whole streaming call is wrapped in a hard timeout so a
        # network blackhole can't leave the runner waiting forever.
        async def _stream() -> AsyncIterator[bytes]:
            async with client.messages.stream(
                model=model,
                max_tokens=max_tokens,
                system=system_prompt,
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                async for chunk in stream.text_stream:
                    if not chunk:
                        continue
                    # Slice if a single chunk is implausibly large.
                    text = chunk
                    while len(text) > _MAX_DELTA_CHARS:
                        yield text_delta(text[:_MAX_DELTA_CHARS])
                        text = text[_MAX_DELTA_CHARS:]
                    if text:
                        yield text_delta(text)

        # Drive the inner generator with a per-frame asyncio.wait_for.
        # If a single frame takes >_TURN_HARD_TIMEOUT_SEC, kill the
        # turn — same defensive pattern as runner.py's idle watchdog.
        agen = _stream().__aiter__()
        while True:
            try:
                frame = await asyncio.wait_for(
                    agen.__anext__(),
                    timeout=_TURN_HARD_TIMEOUT_SEC,
                )
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError:
                logger.error(
                    "[lean-runtime] turn timed out after %ds with no frame",
                    _TURN_HARD_TIMEOUT_SEC,
                )
                yield error(
                    f"lean runtime: no streaming frame in "
                    f"{_TURN_HARD_TIMEOUT_SEC // 60}min — anthropic API hung. retry."
                )
                break
            yield frame

    except asyncio.CancelledError:
        logger.info("[lean-runtime] cancelled by client")
        raise
    except Exception as e:
        logger.exception("[lean-runtime] run_lean_turn raised")
        yield error(f"lean runtime error ({type(e).__name__}): {e}")

    duration_ms = int((time.monotonic() - started) * 1000)
    yield done(duration_ms=duration_ms)
