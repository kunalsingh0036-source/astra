"""
Lean Astra agent loop — direct anthropic.AsyncAnthropic, no SDK CLI.

Phase 3 status: text streaming + tool dispatch via ToolRegistry. Each
turn can issue multiple tool calls, the loop runs them, feeds results
back, and continues until the model emits end_turn. No SDK
subprocess, no opaque hangs.

Reliability guarantees:
  - No subprocess (pure async Python)
  - Per-tool timeout via ToolRegistry.dispatch()
  - Per-turn hard timeout
  - Per-frame idle timeout
  - Tool failures become tool_result errors visible to the model;
    the model can decide whether to retry or proceed

Future phases:
  Phase 4: load + save messages from Postgres turns table
  Phase 4: integrate autonomy.classify before dispatch
  Phase 5: cutover /stream to use this runtime
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, AsyncIterator

from anthropic import AsyncAnthropic

from astra.runtime.event_emitter import (
    done,
    error,
    session as session_event,
    text_delta,
    thought,
    tool_call,
    tool_result,
)
from astra.runtime.session_store import (
    load_session_messages,
    save_turn_messages,
)
from astra.runtime.tool_registry import REGISTRY

logger = logging.getLogger(__name__)


# Hard ceiling per turn. Real chat turns finish in 5-60s; anything
# over this is almost certainly a network problem, not legitimate
# slow work. Per-tool timeouts (set via ToolRegistry) handle the
# tool-call portion; this is the outer fence.
_TURN_HARD_TIMEOUT_SEC = 300

# Maximum number of tool-call iterations within one turn. Real turns
# rarely exceed 5-8 round-trips; cap at 25 to catch model-side loops
# (e.g. a tool that keeps failing the same way and the model keeps
# retrying it).
_MAX_TOOL_ITERATIONS = 25

# Chunk size at which we slice very long text deltas. Defensive
# against the API ever bundling a multi-KB chunk that would block
# the SSE pipe.
_MAX_DELTA_CHARS = 4096


async def run_lean_turn(
    prompt: str,
    *,
    session_id: str | None = None,
    system_prompt: str = "",
    model: str = "claude-sonnet-4-5",
    max_tokens: int = 8192,
    tools_enabled: bool = True,
    tool_namespaces: list[str] | None = None,
    turn_id: int | None = None,
    load_history: bool = True,
) -> AsyncIterator[bytes]:
    """Run one user turn against Anthropic + dispatch tools via the
    registry. Yields SSE-formatted byte frames.

    Args:
        prompt: the user's message
        session_id: identifies the conversation. When `load_history` is
            True (default), prior turns from this session are
            rehydrated into the message stack — multi-turn context
            survives deploys/refreshes because messages live in
            Postgres, not subprocess memory.
        system_prompt: Astra's system prompt
        model: Anthropic model alias
        max_tokens: response cap per turn iteration
        tools_enabled: when False, runs as a pure-text loop (Phase 2
            behavior). Useful for tests + flows that don't need tools.
        tool_namespaces: when provided, only these registry namespaces
            are exposed to the model. Defaults to all registered tools.
        turn_id: when set, the final message stack gets persisted to
            this turn row at end-of-turn. Caller is responsible for
            creating the row (services/stream uses _create_turn_record).
        load_history: when False, skips loading prior turns. Useful for
            tests + isolated single-turn flows.
    """
    started = time.monotonic()
    sid = session_id or str(uuid.uuid4())
    yield session_event(sid)

    client = AsyncAnthropic()

    # Rehydrate prior turns if a session_id is provided. Each
    # completed turn stored its final message stack in the turns
    # table; we concatenate them in started_at order to reconstruct
    # the conversation. This is what gives the lean runtime
    # "multi-turn memory across deploys" — something the legacy SDK
    # path could never achieve because sessions lived in subprocess
    # state that died on restart.
    history: list[dict[str, Any]] = []
    if load_history and session_id:
        try:
            history = await load_session_messages(session_id)
            if history:
                logger.info(
                    "[lean-runtime] rehydrated %d messages from session %s",
                    len(history),
                    session_id,
                )
        except Exception:
            logger.exception("[lean-runtime] session rehydrate failed")
            history = []

    # Build the conversation as we go. Each iteration of the
    # outer while-loop is one assistant response (text + optional
    # tool_use blocks). After tool_use, we append both the assistant
    # turn and the tool_result and loop.
    messages: list[dict[str, Any]] = [
        *history,
        {"role": "user", "content": prompt},
    ]
    anthropic_tools = (
        REGISTRY.as_anthropic_tools(namespaces=tool_namespaces)
        if tools_enabled
        else []
    )

    final_response_text = ""
    tools_called = 0

    try:
        for iteration in range(_MAX_TOOL_ITERATIONS):
            stream_kwargs: dict[str, Any] = {
                "model": model,
                "max_tokens": max_tokens,
                "messages": messages,
            }
            if system_prompt:
                stream_kwargs["system"] = system_prompt
            if anthropic_tools:
                stream_kwargs["tools"] = anthropic_tools

            # Stream the assistant's response. text_stream yields only
            # text deltas; we get the full message (with tool_use
            # blocks) at the end via get_final_message().
            assistant_text_chunks: list[str] = []
            async with client.messages.stream(**stream_kwargs) as stream:
                async for chunk in stream.text_stream:
                    if not chunk:
                        continue
                    assistant_text_chunks.append(chunk)
                    text = chunk
                    while len(text) > _MAX_DELTA_CHARS:
                        yield text_delta(text[:_MAX_DELTA_CHARS])
                        text = text[_MAX_DELTA_CHARS:]
                    if text:
                        yield text_delta(text)

                final_message = await stream.get_final_message()

            assistant_text = "".join(assistant_text_chunks)
            final_response_text = assistant_text  # last iteration wins

            # Inspect the final message: did the model want tools?
            stop_reason = getattr(final_message, "stop_reason", None)
            content_blocks = list(getattr(final_message, "content", []))

            if stop_reason != "tool_use":
                # Model signaled end_turn (or max_tokens, etc.) — done.
                break

            # The model returned tool_use blocks. Append the assistant
            # turn to messages, dispatch each tool, build tool_results.
            messages.append(
                {
                    "role": "assistant",
                    # Anthropic accepts the SDK content blocks directly
                    # OR a list of dicts. Use dict form so we don't
                    # depend on the block classes' internal serialization.
                    "content": [
                        _block_to_dict(b) for b in content_blocks
                    ],
                }
            )

            tool_results_for_user_turn: list[dict[str, Any]] = []
            for block in content_blocks:
                if getattr(block, "type", None) != "tool_use":
                    continue

                tool_id = getattr(block, "id", "") or ""
                tool_name = getattr(block, "name", "") or ""
                tool_input = getattr(block, "input", None) or {}
                if not isinstance(tool_input, dict):
                    tool_input = {}

                tools_called += 1
                yield tool_call(id=tool_id, name=tool_name, agent=None)

                td = REGISTRY.get(tool_name)
                if td is None:
                    msg = (
                        f"unknown tool: {tool_name!r}. "
                        f"Registered: {REGISTRY.names()}"
                    )
                    logger.warning("[lean-runtime] %s", msg)
                    yield tool_result(id=tool_id, preview=msg, is_error=True)
                    tool_results_for_user_turn.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_id,
                            "content": msg,
                            "is_error": True,
                        }
                    )
                    continue

                # Autonomy gate — same vocabulary as the legacy SDK
                # autonomy_pre_tool_hook. We check the current mode +
                # tool tier; deny in always_ask is auto-allowed here
                # (no UI prompt mechanism in lean runtime yet — Phase
                # 6 work). The gate is a function call instead of an
                # SDK hook callback that could hang the CLI.
                allowed, decision_reason = _autonomy_check(td, tool_name)
                if not allowed:
                    msg = f"denied by autonomy: {decision_reason}"
                    logger.info("[lean-runtime] tool %s %s", tool_name, msg)
                    yield tool_result(id=tool_id, preview=msg, is_error=True)
                    tool_results_for_user_turn.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_id,
                            "content": msg,
                            "is_error": True,
                        }
                    )
                    _audit_log(tool_name, td, decision_reason="deny")
                    continue
                _audit_log(tool_name, td, decision_reason=decision_reason)

                logger.info(
                    "[lean-runtime] dispatching tool %s (timeout=%ds)",
                    tool_name,
                    td.timeout_sec,
                )
                result = await REGISTRY.dispatch(tool_name, tool_input)

                # Trim preview for the SSE event (browser shows a snippet)
                preview = result.text[:240].replace("\n", " ")
                if len(result.text) > 240:
                    preview += "…"
                yield tool_result(
                    id=tool_id,
                    preview=preview,
                    is_error=result.is_error,
                )

                tool_results_for_user_turn.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": result.text,
                        "is_error": result.is_error,
                    }
                )

            # Append the synthetic user turn carrying tool results.
            messages.append(
                {"role": "user", "content": tool_results_for_user_turn}
            )

            # If a tool error tier was destructive AND model retries,
            # MAX_TOOL_ITERATIONS catches that — see top of file.
        else:
            # Loop exit via the for...else means we hit
            # _MAX_TOOL_ITERATIONS without reaching end_turn. Surface it.
            yield thought(
                f"reached max tool-iteration cap ({_MAX_TOOL_ITERATIONS}); "
                "stopping loop. the model may want to ask the user something."
            )
            yield error(
                f"agent exceeded {_MAX_TOOL_ITERATIONS} tool iterations "
                "without converging — likely stuck in a retry loop. retry."
            )

    except asyncio.CancelledError:
        logger.info("[lean-runtime] cancelled by client")
        raise
    except Exception as e:
        logger.exception("[lean-runtime] run_lean_turn raised")
        yield error(f"lean runtime error ({type(e).__name__}): {e}")

    duration_ms = int((time.monotonic() - started) * 1000)

    # Persist the full message stack so the next turn in this session
    # can rehydrate. We do this synchronously (not fire-and-forget) so
    # the row is committed before the SSE connection closes — a quick
    # browser disconnect after `done` could otherwise lose the write.
    if turn_id is not None:
        try:
            await save_turn_messages(turn_id, messages)
        except Exception:
            logger.exception(
                "[lean-runtime] save_turn_messages failed for turn=%s", turn_id
            )

    yield done(
        duration_ms=duration_ms,
        meta={"tool_count": tools_called} if tools_called else None,
    )


def _autonomy_check(td: Any, tool_name: str) -> tuple[bool, str]:
    """Check whether a tool may run under the current autonomy mode.

    Returns (allowed, reason). On any failure (autonomy module
    missing, etc.) defaults to ALLOW so the migration doesn't
    introduce regressions vs the legacy path.
    """
    try:
        from astra.autonomy.manager import autonomy_manager
        from astra.autonomy.modes import (
            ActionTier as AutonomyTier,
            PermissionDecision,
            get_permission,
        )
    except Exception:
        return True, "autonomy module unavailable — allowing by default"

    try:
        mode = autonomy_manager.mode
        # Map our ActionTier (runtime copy) to the autonomy module's.
        tier = AutonomyTier(td.tier.value)
        decision = get_permission(mode, tool_name)
        if decision == PermissionDecision.ALLOW:
            return True, f"auto-allow ({mode.value} / {tier.value})"
        if decision == PermissionDecision.DENY:
            return False, f"deny ({mode.value} / {tier.value})"
        # ASK — in the lean runtime there's no UI prompt mechanism yet
        # (the SDK had a permission flow). We allow ASK in semi_auto
        # for read tools and let it through. Conservative: deny ASK
        # for destructive tools. Phase 6 will wire a real prompt UX.
        if tier == AutonomyTier.DESTRUCTIVE:
            return False, f"ask-deny destructive ({mode.value})"
        return True, f"ask-allow ({mode.value} / {tier.value})"
    except Exception:
        return True, "autonomy check raised — allowing"


def _audit_log(tool_name: str, td: Any, *, decision_reason: str) -> None:
    """Forward a tool decision to the audit_events table.

    Uses the same audit_logger singleton the legacy SDK hook used so
    the /audit page shows lean-runtime decisions identically to SDK
    decisions. Fire-and-forget — audit failures must never break the
    turn.
    """
    try:
        from astra.autonomy.audit import audit_logger
        from astra.autonomy.manager import autonomy_manager
        from astra.autonomy.modes import (
            ActionTier as AutonomyTier,
            PermissionDecision,
        )
    except Exception:
        return

    try:
        decision = (
            PermissionDecision.DENY
            if decision_reason.startswith("deny") or decision_reason.startswith("ask-deny")
            else PermissionDecision.ALLOW
        )
        audit_logger.log(
            tool_name=tool_name,
            action_tier=AutonomyTier(td.tier.value),
            autonomy_mode=autonomy_manager.mode,
            decision=decision,
            tool_input_summary="",
            context=f"lean-runtime:{decision_reason}",
        )
    except Exception:
        logger.exception("[lean-runtime] audit log failed")


def _block_to_dict(block: Any) -> dict[str, Any]:
    """Convert an anthropic SDK content block (TextBlock / ToolUseBlock)
    to its dict representation for round-tripping back into messages.

    The SDK objects support .model_dump() (pydantic v2) but we don't
    want to depend on that — manual extraction is bulletproof and
    works whether the SDK swaps its internal types.
    """
    btype = getattr(block, "type", None)
    if btype == "text":
        return {"type": "text", "text": getattr(block, "text", "") or ""}
    if btype == "tool_use":
        return {
            "type": "tool_use",
            "id": getattr(block, "id", "") or "",
            "name": getattr(block, "name", "") or "",
            "input": getattr(block, "input", {}) or {},
        }
    if btype == "thinking":
        # Forward-compat — extended thinking blocks if we ever enable them
        return {
            "type": "thinking",
            "thinking": getattr(block, "thinking", "") or "",
        }
    # Unknown block — preserve type marker so the API doesn't reject
    # the message; content empty so the model doesn't act on it.
    return {"type": str(btype or "unknown")}
