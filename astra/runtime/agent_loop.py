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
import json
import logging
import time
import uuid
from typing import Any, AsyncIterator

from anthropic import AsyncAnthropic

from astra.runtime.event_emitter import (
    artifact,
    done,
    error,
    session as session_event,
    text_delta,
    thought,
    tool_call,
    tool_result,
)
from astra.runtime.event_log import record_event
from astra.runtime.turn_store import finalize_turn_record
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
#
# Set to 240s — 60s under Vercel's 300s maxDuration so the runner
# always has time to yield a clean `done`/`error` event before
# Vercel cancels the connection. See docs/timeout_hierarchy.md.
_TURN_HARD_TIMEOUT_SEC = 240


# ── Event emission with durable log ────────────────────────
#
# Every event the loop yields ALSO gets written to the turn_events
# table (record_event). This is what makes the polling architecture
# work: the browser can poll /api/turns/<id>/events for events even
# when the SSE stream is closed (or never opened). The SSE yield is
# kept for streaming consumers; the DB write is the durable record.
#
# Tradeoff: ~5ms per event for the DB write. Text-deltas fire 50-200
# times per turn so this adds up to ~250ms-2s of DB time across a
# turn. Acceptable; if it ever isn't, batch into a queued writer.

# Map factory function → wire event name. We need the name to write
# to turn_events; the factory produces the SSE bytes. Both stay in
# sync because they're in the same dict.
_EVENT_NAMES: dict[Any, str] = {
    session_event: "session",
    thought: "thought",
    text_delta: "text_delta",
    tool_call: "tool_call",
    tool_result: "tool_result",
    artifact: "artifact",
    error: "error",
    done: "done",
}


# ── Artifact sentinel parsing ──────────────────────────────
#
# Artifact-emitting tools (emit_palette, emit_table, emit_draft,
# emit_metric, prepare_preview, screenshot_url) wrap their structured
# payload in a `⟦ASTRA_ARTIFACT⟧...⟦/ASTRA_ARTIFACT⟧` text sentinel
# embedded in their tool-result content. They have to do it this way
# because tool dispatch is synchronous — tools return a ToolResult,
# they can't yield extra events themselves.
#
# So the agent loop unwraps it: scan every tool result text for these
# sentinels, emit a real `artifact` event for each payload found,
# and replace the sentinel with a tiny placeholder before feeding
# the text back to the model. Without this step, palette swatches /
# tables / drafts / screenshots never reach the UI — the sentinel
# just sits in the tool_result preview and the model later claims to
# have rendered something that the user never saw.
_SENTINEL_OPEN = "⟦ASTRA_ARTIFACT⟧"
_SENTINEL_CLOSE = "⟦/ASTRA_ARTIFACT⟧"


def _extract_artifacts(text: str) -> tuple[list[dict[str, Any]], str]:
    """Find every `⟦ASTRA_ARTIFACT⟧{...json...}⟦/ASTRA_ARTIFACT⟧`
    block in `text`. Return:
      - list of parsed payloads (dicts with at least a "type" key)
      - text with each sentinel replaced by a short placeholder so
        the model-facing tool_result content stays small and isn't
        spammed with raw JSON the model already produced

    Defensive against malformed payloads — a JSON parse failure
    logs a warning but leaves the sentinel in place so the model
    sees something rather than nothing. Unclosed sentinels (no
    matching close marker) are similarly left intact.
    """
    if _SENTINEL_OPEN not in text:
        return [], text
    found: list[dict[str, Any]] = []
    pieces: list[str] = []
    i = 0
    while True:
        s = text.find(_SENTINEL_OPEN, i)
        if s < 0:
            pieces.append(text[i:])
            break
        # Keep anything before the open marker as-is.
        pieces.append(text[i:s])
        e = text.find(_SENTINEL_CLOSE, s)
        if e < 0:
            # Unclosed — defensive: keep the rest of the text raw
            # so a partial response doesn't get silently truncated.
            pieces.append(text[s:])
            break
        body = text[s + len(_SENTINEL_OPEN) : e]
        try:
            payload = json.loads(body)
            if isinstance(payload, dict):
                found.append(payload)
                pieces.append("[artifact emitted]")
            else:
                # JSON parsed but isn't a dict — leave verbatim.
                pieces.append(text[s : e + len(_SENTINEL_CLOSE)])
        except json.JSONDecodeError:
            logger.warning(
                "[lean-runtime] artifact sentinel found but body is "
                "not valid JSON (first 80 chars: %r) — passing through",
                body[:80],
            )
            pieces.append(text[s : e + len(_SENTINEL_CLOSE)])
        i = e + len(_SENTINEL_CLOSE)
    return found, "".join(pieces)


async def _emit(
    turn_id: int | None,
    factory: Any,
    **payload: Any,
) -> bytes:
    """Record an event in the durable log + return the SSE bytes.

    Use as `yield await _emit(turn_id, text_delta, content=...)` —
    one source of truth for both the durable record AND the live
    stream. Drift between the two is structurally impossible.
    """
    name = _EVENT_NAMES.get(factory, factory.__name__)
    await record_event(turn_id, name, payload)
    return factory(**payload)

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
    attachments: list[str] | None = None,
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
        attachments: list of upload IDs (UUIDs into the previews table)
            that should be fetched + included as image content blocks
            on the user message. Used by the drag-and-drop screenshot
            feature in InputLine. Empty/None = pure text turn.
    """
    started = time.monotonic()
    sid = session_id or str(uuid.uuid4())
    yield await _emit(turn_id, session_event, session_id=sid)

    # Cross-service mode sync. The web UI's /settings toggle writes
    # to app_settings; this is where the stream service picks it up.
    # Without this refresh, an in-memory mode set when the container
    # booted ("always_ask" by default) silently overrides whatever
    # the user chose in the UI — the exact symptom of "I switched
    # to semi-auto but the agent keeps asking permission." Best-
    # effort; a DB blip leaves the previous mode in place.
    try:
        from astra.autonomy.manager import autonomy_manager
        await autonomy_manager.refresh_from_db()
    except Exception:
        logger.exception("[lean-runtime] autonomy refresh_from_db failed")

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
    #
    # User-message content shape depends on attachments:
    #   - text-only: a plain string (matches the historical default)
    #   - with images: a list of content blocks per Anthropic vision
    #     spec, prompt-text last so the model sees the images first
    #     then reads what to do with them
    user_content: Any = prompt
    if attachments:
        user_content = await _build_user_content_with_attachments(
            prompt, attachments
        )
    raw_messages: list[dict[str, Any]] = [
        *history,
        {"role": "user", "content": user_content},
    ]
    # Compact before sending to the API. Without this, sessions that
    # have accumulated large tool_result content (file reads, glob
    # dumps, web fetches) blow past the 200k context window. The
    # compactor truncates oversized tool_results first, then drops
    # oldest turns, all while preserving role-alternation AND
    # tool_use/tool_result atomicity (see _compact_messages docstring).
    messages, before_tokens, after_tokens = _compact_messages(raw_messages)
    if after_tokens != before_tokens:
        logger.info(
            "[lean-runtime] compacted messages: %d → %d tokens (~%.0f%%)",
            before_tokens,
            after_tokens,
            (1 - after_tokens / max(before_tokens, 1)) * 100,
        )
    # Defense in depth: drop any tool_result blocks whose tool_use is
    # missing. Compaction is supposed to keep pairs intact, but if
    # anything upstream (custom recall path, future refactor) produces
    # orphans we'd rather lose a tool result than fail the entire turn.
    messages = _validate_and_repair_messages(messages)
    # The user's prompt for THIS turn is the LAST message after
    # compaction (compactor preserves the tail; nothing has been
    # appended yet). Track its index so save_turn_messages can store
    # only THIS turn's contribution — not the loaded history. Without
    # this, every turn stored its full history, producing quadratic
    # duplication that forced compaction prematurely (turn #51 stored
    # 104 messages including all of turns 44-50; turn #53 then loaded
    # 234 messages from concatenating prior turns and crashed).
    own_start_index = max(0, len(messages) - 1)
    anthropic_tools = (
        REGISTRY.as_anthropic_tools(namespaces=tool_namespaces)
        if tools_enabled
        else []
    )

    final_response_text = ""
    tools_called = 0
    # Cumulative across all iterations — final_response_text gets
    # overwritten each iteration ("last iteration wins" for the DB
    # row), but for the "did the user actually see anything?" check
    # below we need the totals across the whole turn.
    total_text_chars = 0
    artifacts_emitted = 0
    last_stop_reason: str | None = None
    turn_status = "complete"
    turn_error: str | None = None

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
                    total_text_chars += len(chunk)
                    text = chunk
                    while len(text) > _MAX_DELTA_CHARS:
                        chunk_to_emit = text[:_MAX_DELTA_CHARS]
                        yield await _emit(
                            turn_id, text_delta, content=chunk_to_emit
                        )
                        text = text[_MAX_DELTA_CHARS:]
                    if text:
                        yield await _emit(turn_id, text_delta, content=text)

                final_message = await stream.get_final_message()

            assistant_text = "".join(assistant_text_chunks)
            final_response_text = assistant_text  # last iteration wins

            # Inspect the final message: did the model want tools?
            stop_reason = getattr(final_message, "stop_reason", None)
            last_stop_reason = stop_reason
            content_blocks = list(getattr(final_message, "content", []))

            if stop_reason != "tool_use":
                # Model signaled end_turn, max_tokens, refusal, etc.
                # — done. Surface the unusual exits so the user knows
                # the turn ended for a reason other than "model
                # finished its answer".
                if stop_reason == "max_tokens":
                    # Truncation. The text/tool stream got cut off
                    # mid-output. The model wanted to say more but
                    # ran out of token budget for this iteration.
                    yield await _emit(
                        turn_id,
                        error,
                        message=(
                            "response was truncated (hit max_tokens). "
                            "ask me to continue or break the request "
                            "into smaller parts."
                        ),
                    )
                elif stop_reason == "refusal":
                    # Anthropic refused to complete (safety, policy).
                    # The user gets nothing useful by default — say so.
                    yield await _emit(
                        turn_id,
                        error,
                        message=(
                            "the model refused to answer this turn "
                            "(safety filter). rephrase or split into "
                            "smaller asks."
                        ),
                    )
                elif stop_reason and stop_reason not in {
                    "end_turn",
                    "stop_sequence",
                }:
                    # Anything we didn't explicitly handle — log so the
                    # user has a clue why output might be incomplete.
                    logger.warning(
                        "[lean-runtime] unusual stop_reason=%r — "
                        "treating as end of turn",
                        stop_reason,
                    )
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
                yield await _emit(
                    turn_id, tool_call, id=tool_id, name=tool_name, agent=None
                )

                td = REGISTRY.get(tool_name)
                if td is None:
                    msg = (
                        f"unknown tool: {tool_name!r}. "
                        f"Registered: {REGISTRY.names()}"
                    )
                    logger.warning("[lean-runtime] %s", msg)
                    yield await _emit(
                        turn_id, tool_result, id=tool_id, preview=msg, is_error=True
                    )
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
                    yield await _emit(
                        turn_id, tool_result, id=tool_id, preview=msg, is_error=True
                    )
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

                # Unwrap artifact sentinels — tools like emit_palette,
                # emit_table, screenshot_url stuff a structured payload
                # into their text result wrapped in ⟦ASTRA_ARTIFACT⟧...
                # markers. We turn each one into a real `artifact`
                # event the UI can render (swatches, tables, image
                # tiles) and scrub the marker out of the text before
                # it reaches the model again.
                artifacts_found, scrubbed_text = _extract_artifacts(
                    result.text
                )
                for art_payload in artifacts_found:
                    yield await _emit(
                        turn_id,
                        artifact,
                        type=str(art_payload.get("type") or "unknown"),
                        title=art_payload.get("title")
                        or art_payload.get("name"),
                        content=art_payload,
                    )
                    artifacts_emitted += 1

                # Trim preview for the SSE event (browser shows a snippet)
                preview = scrubbed_text[:240].replace("\n", " ")
                if len(scrubbed_text) > 240:
                    preview += "…"
                yield await _emit(
                    turn_id,
                    tool_result,
                    id=tool_id,
                    preview=preview,
                    is_error=result.is_error,
                )

                tool_results_for_user_turn.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": scrubbed_text,
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
            turn_status = "failed"
            turn_error = (
                f"exceeded {_MAX_TOOL_ITERATIONS} tool iterations "
                "without converging"
            )
            yield await _emit(
                turn_id,
                thought,
                text=(
                    f"reached max tool-iteration cap ({_MAX_TOOL_ITERATIONS}); "
                    "stopping loop. the model may want to ask the user something."
                ),
            )
            yield await _emit(
                turn_id,
                error,
                message=(
                    f"agent exceeded {_MAX_TOOL_ITERATIONS} tool iterations "
                    "without converging — likely stuck in a retry loop. retry."
                ),
            )

        # Empty-output guard. The loop above can exit cleanly
        # (status="complete") with nothing the user can see: the
        # model recursed through read-only tools like recall_memories
        # without ever emitting text or an artifact, then end_turn'd.
        # From the user's POV the UI shows "answered in 26s" with a
        # blank pane and a "wat?" feeling. Surface a real message
        # instead so they know to rephrase, not retry blindly.
        if (
            turn_status == "complete"
            and total_text_chars == 0
            and artifacts_emitted == 0
        ):
            msg = (
                "i finished without producing a response — likely got "
                "stuck looking up context. try rephrasing or giving me "
                "more specifics about what you want."
            )
            if last_stop_reason and last_stop_reason not in {
                "end_turn",
                "stop_sequence",
            }:
                msg += f" (stop_reason: {last_stop_reason})"
            logger.warning(
                "[lean-runtime] empty turn — tools=%d, stop_reason=%r",
                tools_called,
                last_stop_reason,
            )
            yield await _emit(turn_id, error, message=msg)

    except asyncio.CancelledError:
        logger.info("[lean-runtime] cancelled by client")
        turn_status = "interrupted"
        turn_error = "client cancelled"
        # Best-effort finalize before re-raising so the row reflects
        # reality even on cancellation.
        if turn_id is not None:
            try:
                await save_turn_messages(turn_id, messages[own_start_index:])
                await finalize_turn_record(
                    turn_id,
                    session_id=sid,
                    response=final_response_text,
                    status=turn_status,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    cost_usd=None,
                    tool_count=tools_called,
                    error_message=turn_error,
                )
            except Exception:
                pass
        raise
    except Exception as e:
        logger.exception("[lean-runtime] run_lean_turn raised")
        turn_status = "failed"
        turn_error = f"{type(e).__name__}: {e}"
        yield await _emit(
            turn_id,
            error,
            message=f"lean runtime error ({type(e).__name__}): {e}",
        )

    duration_ms = int((time.monotonic() - started) * 1000)

    # Persist this turn's OWN contribution (user prompt + assistant
    # responses + tool results from THIS turn) — NOT the rehydrated
    # history above. Otherwise each turn's stored messages would
    # include all prior turns, and load_session_messages would
    # concatenate the duplicated stacks producing quadratic blow-up
    # (turn #51 stored 104 messages, turn #53 loaded 234, compaction
    # split a tool_use/tool_result pair, Anthropic rejected the call).
    #
    # Plus the row's status/response/duration/tool_count so
    # load_session_messages' WHERE status='complete' filter passes.
    # The legacy SDK runner did this via _finalize_turn_record; the
    # lean runtime forgot to wire it up — Phase 4 was incomplete and
    # every turn stayed at status='running' forever, breaking session
    # continuity. Both writes are synchronous so the row is committed
    # before the response is finalized.
    if turn_id is not None:
        try:
            await save_turn_messages(turn_id, messages[own_start_index:])
        except Exception:
            logger.exception(
                "[lean-runtime] save_turn_messages failed for turn=%s", turn_id
            )
        try:
            await finalize_turn_record(
                turn_id,
                session_id=sid,
                response=final_response_text,
                status=turn_status,
                duration_ms=duration_ms,
                cost_usd=None,
                tool_count=tools_called,
                error_message=turn_error,
            )
        except Exception:
            logger.exception(
                "[lean-runtime] finalize_turn_record failed for turn=%s", turn_id
            )

        # Fire-and-forget topic title generation. The function is
        # idempotent (skips if a title already exists) and runs
        # asyncio.create_task so it doesn't add latency to the
        # response. First-turn-of-session check happens inside the
        # generator; bridging here keeps the agent_loop simple.
        if sid and turn_status == "complete":
            try:
                from astra.runtime.session_title import fire_and_forget as fire_title
                fire_title(sid)
            except Exception:
                logger.exception(
                    "[lean-runtime] session-title fire-and-forget failed"
                )

    done_payload: dict[str, Any] = {"duration_ms": duration_ms}
    if tools_called:
        done_payload["meta"] = {"tool_count": tools_called}
    yield await _emit(turn_id, done, **done_payload)


# ── Attachments → content blocks ───────────────────────────


# Mirrors the stream-service _ALLOWED_UPLOAD_TYPES set. Anthropic
# Sonnet 4.5 accepts these as image source media_types per their
# vision spec. WebP works; HEIC/AVIF don't. Server-side guards
# against everything else at upload time, so reaching this fn with
# a non-image content type is a bug — log and skip.
_VISION_MEDIA_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}


async def _build_user_content_with_attachments(
    prompt: str, attachment_ids: list[str]
) -> list[dict[str, Any]]:
    """Fetch each upload by id, return Anthropic-shaped content
    blocks. Order is image-first, prompt-text last — Anthropic's
    vision guide recommends putting images BEFORE text so the model
    sees them before reading the question.

    Failures are degraded: if an upload row is missing or expired,
    we skip it (don't fail the whole turn) and log a warning so
    debugging is possible. The prompt itself is always sent.
    """
    from astra.runtime.preview_store import get_preview

    blocks: list[dict[str, Any]] = []
    for upload_id in attachment_ids:
        row = await get_preview(upload_id)
        if not row:
            logger.warning(
                "[lean-runtime] attachment %s missing/expired — skipping",
                upload_id,
            )
            continue
        media_type = row.get("content_type", "")
        if media_type not in _VISION_MEDIA_TYPES:
            logger.warning(
                "[lean-runtime] attachment %s unsupported media_type %r — skipping",
                upload_id,
                media_type,
            )
            continue
        # Body is base64-encoded text (stored as-is in the previews
        # table TEXT column). Anthropic's API accepts base64 directly
        # in source.data so we can pass it through without round-
        # tripping to bytes and back.
        blocks.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": row["body"],
                },
            }
        )
    # Prompt text comes last so the model sees images first and the
    # instruction immediately after — better than the inverse per
    # Anthropic's vision examples.
    if prompt:
        blocks.append({"type": "text", "text": prompt})
    return blocks


# ── Context-window management ──────────────────────────────
#
# Claude's hard limit is 200k tokens. Session histories that include
# large tool_result content (file reads, web fetches, glob dumps)
# routinely cross 200k after enough turns. Without compaction, the
# next API call returns 400 BadRequestError("prompt is too long")
# and the user is locked out of the session.
#
# The compactor runs every turn before we send to the API. It NEVER
# touches stored messages — only the in-flight copy. Future turns
# continue to load the full history; each turn re-runs compaction
# against the latest state.

# Soft target for the MESSAGES portion of the API call. The full API
# call also carries:
#   - system prompt (~10k tokens for Astra's prompt)
#   - tools list (input_schema + description × 100+ tools ≈ ~10-15k)
#   - new prompt + response budget (~5-10k)
# Plus the char-based estimator undercounts JSON-heavy tool-call
# content by ~25-30%. So a 180k char-based estimate can be 213k
# actual API tokens (real production hit). We target 130k chars-
# estimated to leave room for everything else under the 200k cap.
_COMPACT_TARGET_TOKENS = 130_000

# Per-tool-result content cap. Above this we replace with a head +
# truncation marker. 2KB ≈ 500 tokens — large enough to preserve
# what the model needs to reason about + understand it was clipped,
# small enough that 50 of them stay well under budget.
_TOOL_RESULT_CAP_CHARS = 2_000


def _estimate_tokens_for_block(block: Any) -> int:
    """Rough character→token ratio. ~4 chars per token is the standard
    English approximation for tiktoken-style BPE; close enough for
    budget triage. We don't need exactness — we just need to know
    whether we're under the limit, and if not, what to drop first."""
    if isinstance(block, str):
        return len(block) // 4
    if isinstance(block, dict):
        # text blocks
        if "text" in block and isinstance(block["text"], str):
            return len(block["text"]) // 4
        # tool_use blocks: name + serialized input
        if block.get("type") == "tool_use":
            n = len(block.get("name", "")) // 4
            input_str = str(block.get("input", ""))
            return n + len(input_str) // 4
        # tool_result blocks: content can be string OR list of text blocks
        if block.get("type") == "tool_result":
            c = block.get("content")
            if isinstance(c, str):
                return len(c) // 4
            if isinstance(c, list):
                return sum(_estimate_tokens_for_block(b) for b in c)
        # fallback — count the JSON-encoded length
        try:
            import json as _json
            return len(_json.dumps(block)) // 4
        except Exception:
            return 100
    if isinstance(block, list):
        return sum(_estimate_tokens_for_block(b) for b in block)
    return 0


def _estimate_tokens_for_message(msg: dict[str, Any]) -> int:
    return _estimate_tokens_for_block(msg.get("content")) + 4  # role overhead


def _truncate_tool_result_content(content: Any) -> tuple[Any, bool]:
    """Truncate large tool_result content. Returns (new_content, did_truncate)."""
    if isinstance(content, str):
        if len(content) > _TOOL_RESULT_CAP_CHARS:
            head = content[:_TOOL_RESULT_CAP_CHARS]
            return (
                head + f"\n…[tool_result truncated; was {len(content)} chars]",
                True,
            )
        return content, False
    if isinstance(content, list):
        new_blocks: list[Any] = []
        any_changed = False
        for b in content:
            if isinstance(b, dict) and "text" in b:
                t = b.get("text", "")
                if isinstance(t, str) and len(t) > _TOOL_RESULT_CAP_CHARS:
                    nb = dict(b)
                    nb["text"] = (
                        t[:_TOOL_RESULT_CAP_CHARS]
                        + f"\n…[tool_result truncated; was {len(t)} chars]"
                    )
                    new_blocks.append(nb)
                    any_changed = True
                    continue
            new_blocks.append(b)
        return new_blocks, any_changed
    return content, False


def _message_has_tool_result(msg: dict[str, Any]) -> bool:
    """True if `msg`'s content contains any tool_result block."""
    c = msg.get("content")
    if not isinstance(c, list):
        return False
    return any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in c
    )


def _validate_and_repair_messages(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Drop any tool_result block whose matching tool_use is missing
    from the immediately previous message.

    Anthropic's API requires every tool_result.tool_use_id to have
    a corresponding tool_use.id in the prior message — otherwise it
    rejects with a 400 BadRequestError that aborts the entire turn.

    This is the third layer of defense (after compaction tool-pair
    atomicity and per-turn save-only-own-contribution). If anything
    upstream produces orphans we'd rather drop the orphan than fail
    the turn — losing one tool result is recoverable, a failed turn
    isn't.

    A user message that ends up with NO blocks after dropping is
    replaced by a synthetic text-only user message so role
    alternation stays valid.
    """
    repaired: list[dict[str, Any]] = []
    available_tool_use_ids: set[str] = set()
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            repaired.append(msg)
            available_tool_use_ids = set()
            continue
        new_blocks: list[Any] = []
        dropped_any = False
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                tid = b.get("tool_use_id")
                if tid not in available_tool_use_ids:
                    logger.warning(
                        "[lean-runtime] dropped orphaned tool_result "
                        "tool_use_id=%s (no matching tool_use in prior "
                        "message)",
                        tid,
                    )
                    dropped_any = True
                    continue
            new_blocks.append(b)
        new_msg = dict(msg)
        if not new_blocks and dropped_any:
            # Replace the empty user-with-only-orphans message with
            # a synthetic text note so role alternation stays valid.
            new_msg["content"] = [
                {
                    "type": "text",
                    "text": (
                        "[earlier tool result dropped during compaction; "
                        "re-call the tool if the result is needed]"
                    ),
                }
            ]
        else:
            new_msg["content"] = new_blocks if new_blocks else content
        repaired.append(new_msg)
        # Track tool_use IDs available to the NEXT message's tool_results
        available_tool_use_ids = set()
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                available_tool_use_ids.add(b.get("id"))
    return repaired


def _compact_messages(
    messages: list[dict[str, Any]],
    *,
    max_tokens: int = _COMPACT_TARGET_TOKENS,
) -> tuple[list[dict[str, Any]], int, int]:
    """Compact a message stack to fit within max_tokens.

    Two passes:
      1. Truncate oversized tool_result content (most token-dense).
      2. If still over budget, drop oldest user/assistant pairs while
         preserving role alternation. Always keeps the LAST user
         message (the in-flight prompt) and the FIRST user message
         (anchors the session's intent).

    Returns: (new_messages, before_tokens, after_tokens)
    Both estimates are character-based approximations.
    """
    before = sum(_estimate_tokens_for_message(m) for m in messages)
    # Early exit only when BOTH the token estimate is comfortable AND
    # the message count is reasonable. Long sessions with 2000+ tiny
    # messages still need pass 2 because the actual API token count
    # (after system prompt + tools list) routinely exceeds our
    # char-based estimate by 25-30%.
    if before <= max_tokens and len(messages) <= 200:
        return messages, before, before

    # ── Pass 1: truncate tool_result content ──
    pass1: list[dict[str, Any]] = []
    for msg in messages:
        new_msg = dict(msg)
        c = new_msg.get("content")
        if isinstance(c, list):
            new_blocks: list[Any] = []
            for b in c:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    nb = dict(b)
                    nb["content"], _ = _truncate_tool_result_content(
                        nb.get("content")
                    )
                    new_blocks.append(nb)
                else:
                    new_blocks.append(b)
            new_msg["content"] = new_blocks
        pass1.append(new_msg)

    pass1_tokens = sum(_estimate_tokens_for_message(m) for m in pass1)
    # Trigger pass 2 if either the token estimate is over budget OR
    # the message count is high enough that it's likely going to be
    # over once the system prompt + tools list are added on the
    # server side. 200 messages is the empirical threshold — sessions
    # with that many tool iterations pretty much always benefit from
    # a hard tail-only window.
    if pass1_tokens <= max_tokens and len(pass1) <= 200:
        return pass1, before, pass1_tokens

    # ── Pass 2: drop oldest messages, keep the bookends ──
    # The conversation alternates user/assistant. We keep:
    #   - The very first user message (sets up what the session is for)
    #   - The last 8 messages (recent context — usually 4 user/assistant
    #     turn pairs)
    # And drop everything in between, replaced by a single synthetic
    # user note explaining the gap so the model knows it happened.
    if len(pass1) <= 10:
        # Already short — can't drop more. Return what we have; the
        # API call will fail loudly with the actual overrun, which is
        # easier to debug than silent context loss.
        return pass1, before, pass1_tokens

    head = pass1[:1]  # first user message
    # The naive tail of the last 8 messages can split a tool_use /
    # tool_result pair: assistant(tool_use) lives at index N,
    # user(tool_result) at N+1. If our slice starts at N+1 we keep
    # the tool_result but drop the tool_use → Anthropic rejects with
    # "unexpected tool_use_id found in tool_result blocks". This
    # happened in production (turn #53 of session a859083e…) and was
    # the entire bug class.
    #
    # Walk the start index backward through any user message whose
    # content includes tool_result blocks — its matching tool_use is
    # in the IMMEDIATELY PREVIOUS assistant message, which we then
    # also include. Bounded by 1 doubling of tail size so a pathological
    # all-tool-result run can't grow the slice unboundedly.
    tail_start = len(pass1) - 8
    max_tail_start = max(1, len(pass1) - 16)
    while tail_start > max_tail_start:
        msg = pass1[tail_start]
        if not _message_has_tool_result(msg):
            break
        tail_start -= 1
    tail = pass1[tail_start:]
    elided = len(pass1) - len(head) - len(tail)
    gap_marker: dict[str, Any] = {
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": (
                    f"[context window: {elided} earlier messages were elided "
                    f"to fit Claude's 200k token limit. The conversation "
                    f"continued for several more turns between the message "
                    f"above and the messages below. If you need details from "
                    f"that gap, ask me to recall a specific topic or "
                    f"call recall_recent_turns.]"
                ),
            }
        ],
    }
    # Assemble head + bridges + gap_marker + tail with valid
    # user/assistant alternation throughout. Anthropic rejects
    # consecutive same-role messages (and rejects user→user or
    # assistant→assistant transitions). Bridges are minimal text-
    # only stubs that carry no semantic content beyond filling the
    # alternation slot.
    #
    # The previous logic produced u/u/u sequences in the case where
    # tail started with user — it inserted gap_marker between
    # head[-1] (user) and tail[0] (user) without considering
    # alternation. That's now fixed by the helper below.
    bridge_assistant: dict[str, Any] = {
        "role": "assistant",
        "content": [{"type": "text", "text": "[continuing earlier session]"}],
    }
    bridge_user: dict[str, Any] = {
        "role": "user",
        "content": [{"type": "text", "text": "[continuing]"}],
    }
    compacted = list(head)
    # Bridge head[-1] → gap_marker (user): need assistant between if
    # head[-1] is user.
    if compacted and compacted[-1].get("role") == "user":
        compacted.append(bridge_assistant)
    compacted.append(gap_marker)  # user role
    # Bridge gap_marker (user) → tail[0]: need assistant between if
    # tail[0] is user.
    if tail and tail[0].get("role") == "user":
        compacted.append(bridge_assistant)
    compacted.extend(tail)
    # Final pass: defensively ensure no same-role adjacent messages
    # remain (e.g. tail itself contains u/u). Insert minimal bridges
    # to fix. This handles the edge case where the historical message
    # stack itself has alternation gaps from older bugs.
    fixed: list[dict[str, Any]] = []
    for msg in compacted:
        if fixed and fixed[-1].get("role") == msg.get("role"):
            fixed.append(
                bridge_assistant if msg.get("role") == "user" else bridge_user
            )
        fixed.append(msg)
    compacted = fixed

    final_tokens = sum(_estimate_tokens_for_message(m) for m in compacted)
    return compacted, before, final_tokens


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
