"""
Astra runner — one-shot query execution with SSE event streaming.

Bridges the Claude Agent SDK's message protocol to the Astra stream
event vocabulary. For each SDK message we emit zero or more SSE events
that the browser knows how to render.

Translation table
-----------------

    AssistantMessage                  → text_delta (for each TextBlock)
                                      → tool_call   (for each ToolUseBlock)
                                      → thought     (for each ThinkingBlock)
    UserMessage (tool result echo)    → tool_result
    SystemMessage                     → thought    (only when content seems user-facing)
    ResultMessage                     → done

Errors from the SDK or from underlying agents become `error` events
before closing the stream cleanly.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from typing import AsyncIterator

logger = logging.getLogger(__name__)

# Must stay in sync with astra.tools.artifact_tools
ARTIFACT_SENTINEL_OPEN = "⟦ASTRA_ARTIFACT⟧"
ARTIFACT_SENTINEL_CLOSE = "⟦/ASTRA_ARTIFACT⟧"
_ARTIFACT_RE = re.compile(
    re.escape(ARTIFACT_SENTINEL_OPEN) + r"(.*?)" + re.escape(ARTIFACT_SENTINEL_CLOSE),
    re.DOTALL,
)


def _preview(value: object, limit: int = 200) -> str:
    """Truncate a tool result for display. Never raises."""
    try:
        text = value if isinstance(value, str) else str(value)
    except Exception:
        return "<unreadable>"
    text = text.strip().replace("\n", " ")
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def _agent_from_tool(tool_name: str) -> str | None:
    """
    Guess which agent an MCP tool belongs to from its name.

    SDK tool names look like `mcp__astra-a2a__send_a2a_task`. For A2A
    calls we can't know the target agent from the name alone — we'd
    have to inspect the args. That's a Phase 4 refinement.
    """
    if tool_name.startswith("mcp__astra-"):
        parts = tool_name.split("__")
        if len(parts) >= 2:
            return parts[1].replace("astra-", "")
    return None


_AUTONOMY_ALLOWED = {"always_ask", "semi_auto", "full_auto"}


def _autonomy_file_path() -> str:
    """File-based override path (LOCAL DEV ONLY).

    On Railway the source of truth is the shared Postgres app_settings
    table — files don't sync across containers. The file path is kept
    so single-host local installs continue to work without DB access."""
    import os as _os
    env = _os.environ.get("ASTRA_AUTONOMY_FILE", "").strip()
    if env:
        return env
    home = _os.environ.get("HOME", "/tmp")
    return _os.path.join(home, ".astra-state", "autonomy_mode.txt")


_LEGACY_AUTONOMY_FILE = (
    "/Users/kunalsingh/Claude Code/astra-control/autonomy_mode.txt"
)


async def _read_autonomy_from_db() -> str | None:
    """Read the autonomy mode from Postgres. Returns None on any error
    (including 'table does not exist' pre-migration) so the file
    fallback can still work."""
    try:
        from sqlalchemy import text  # type: ignore[import-not-found]
        from astra.db.engine import async_session  # type: ignore[import-not-found]
    except Exception:
        return None
    try:
        async with async_session() as s:
            r = await s.execute(
                text("SELECT value FROM app_settings WHERE key = 'autonomy_mode'")
            )
            row = r.first()
            if row and row[0] in _AUTONOMY_ALLOWED:
                return str(row[0])
    except Exception:
        return None
    return None


def _read_autonomy_from_file() -> str | None:
    """File-based fallback for local dev."""
    for path in (_autonomy_file_path(), _LEGACY_AUTONOMY_FILE):
        try:
            with open(path, encoding="utf8") as f:
                mode = f.read().strip()
            if mode in _AUTONOMY_ALLOWED:
                return mode
        except FileNotFoundError:
            continue
        except Exception:
            continue
    return None


async def _read_autonomy_override() -> str | None:
    """Read the UI-set autonomy mode. Resolution order:
      1. Postgres app_settings (production — shared across services)
      2. File-based override (local dev — single host)
      3. None — caller falls back to the manager's default
    """
    db_mode = await _read_autonomy_from_db()
    if db_mode:
        return db_mode
    return _read_autonomy_from_file()


async def run_query(
    prompt: str,
    *,
    resume_session_id: str | None = None,
) -> AsyncIterator[bytes]:
    """
    Execute one user turn against Astra and yield SSE event bytes.

    Args:
        prompt: The user's message for this turn.
        resume_session_id: If provided, the Agent SDK resumes that
            session and the model keeps prior conversation context.

    The caller (the FastAPI endpoint) just awaits this generator and
    forwards each frame to the HTTP response.
    """
    # Lazy imports so the service boots even if Astra core isn't yet
    # importable — we emit a clean error instead of crashing.
    try:
        from claude_agent_sdk import (  # type: ignore[import-not-found]
            ClaudeSDKClient,
            AssistantMessage,
            UserMessage,
            ResultMessage,
            TextBlock,
            ToolUseBlock,
            ToolResultBlock,
            ThinkingBlock,
        )
        from astra.core.agent import create_astra_options  # type: ignore[import-not-found]
        from astra.autonomy.manager import autonomy_manager  # type: ignore[import-not-found]
        from astra.autonomy.modes import AutonomyMode  # type: ignore[import-not-found]
        from astra.telemetry import record_usage  # type: ignore[import-not-found]
    except Exception as e:
        from stream.events import error, done

        yield error(f"failed to load astra core: {e}")
        yield done(duration_ms=0)
        return

    from stream.events import (
        session,
        thought,
        tool_call,
        tool_result,
        text_delta,
        artifact as artifact_event,
        done,
        error,
    )

    # Emit an initial session id so the browser has something to show
    # in the status bar. If we're resuming, advertise the resumed id;
    # otherwise generate a placeholder (the SDK will emit the real one
    # on its first AssistantMessage, and subsequent turns will use that
    # canonical id when the browser sends it back).
    session_id = resume_session_id or str(uuid.uuid4())
    yield session(session_id)

    started = time.monotonic()

    # Honor the UI-set autonomy override each turn. If no override is
    # on disk, keep whatever mode the manager already holds (likely
    # the settings default). The autonomy hooks consult this singleton
    # on every tool call, so this takes effect immediately for the
    # turn we're about to run.
    override = await _read_autonomy_override()
    if override:
        try:
            autonomy_manager.set_mode(
                AutonomyMode(override),
                reason="ui override",
            )
        except Exception:
            logger.exception("failed to apply autonomy override %r", override)

    try:
        options = create_astra_options(resume_session_id=resume_session_id)
    except Exception as e:
        logger.exception("create_astra_options failed")
        yield error(f"failed to configure astra: {e}")
        yield done(duration_ms=int((time.monotonic() - started) * 1000))
        return

    # Run Astra and translate messages → events
    canonical_session_emitted = False
    try:
        async with ClaudeSDKClient(options=options) as client:
            await client.query(prompt)

            async for message in client.receive_response():
                # When the SDK assigns its own session_id (first assistant
                # or user echo message), forward it so the browser has the
                # canonical id to send back for the next turn.
                if not canonical_session_emitted:
                    sdk_sid = getattr(message, "session_id", None)
                    if sdk_sid and sdk_sid != session_id:
                        session_id = sdk_sid
                        yield session(session_id)
                        canonical_session_emitted = True
                    elif sdk_sid:
                        canonical_session_emitted = True

                if isinstance(message, AssistantMessage):
                    for block in getattr(message, "content", []) or []:
                        if isinstance(block, ThinkingBlock):
                            # Thinking is optional; some models don't emit it.
                            text = getattr(block, "thinking", None)
                            if text:
                                yield thought(_preview(text, limit=220))
                        elif isinstance(block, TextBlock):
                            text = getattr(block, "text", "") or ""
                            if text:
                                yield text_delta(text)
                        elif isinstance(block, ToolUseBlock):
                            tool_id = getattr(block, "id", "") or ""
                            tool_name = getattr(block, "name", "") or ""
                            agent = _agent_from_tool(tool_name)
                            yield tool_call(id=tool_id, name=tool_name, agent=agent)

                elif isinstance(message, UserMessage):
                    # The SDK echoes tool results as UserMessage frames.
                    for block in getattr(message, "content", []) or []:
                        if isinstance(block, ToolResultBlock):
                            tool_id = getattr(block, "tool_use_id", "") or ""
                            content = getattr(block, "content", "") or ""
                            is_error = bool(getattr(block, "is_error", False))
                            # content can be string | list[TextBlock] | list[dict]
                            if isinstance(content, list):
                                parts: list[str] = []
                                for b in content:
                                    text_val = getattr(b, "text", None)
                                    if text_val is None and isinstance(b, dict):
                                        text_val = b.get("text")
                                    if text_val:
                                        parts.append(str(text_val))
                                content = " ".join(parts)
                            logger.info(
                                "tool_result id=%s len=%d head=%r",
                                tool_id, len(content or ""), (content or "")[:80],
                            )

                            # Artifact tools embed a sentinel-wrapped JSON
                            # payload. Extract them and emit as dedicated
                            # events; leave the rest as tool_result frames.
                            text = content if isinstance(content, str) else str(content)
                            artifacts_found = False
                            for match in _ARTIFACT_RE.finditer(text):
                                artifacts_found = True
                                try:
                                    payload = json.loads(match.group(1))
                                except json.JSONDecodeError:
                                    continue
                                yield artifact_event(
                                    type=str(payload.get("type") or "unknown"),
                                    title=payload.get("title"),
                                    content=payload,
                                )

                            # If the result was *only* artifacts, suppress
                            # the raw tool_result preview (otherwise the
                            # sentinel text shows up in the debug feed).
                            stripped = _ARTIFACT_RE.sub("", text).strip()
                            if artifacts_found and not stripped:
                                continue

                            yield tool_result(
                                id=tool_id,
                                preview=_preview(stripped or text),
                                is_error=is_error,
                            )

                elif isinstance(message, ResultMessage):
                    # End-of-turn marker. Persist usage (fire and forget so
                    # a slow DB doesn't block the final `done` frame) and
                    # carry the cost summary forward for the done event.
                    final_result = message
                    asyncio.create_task(record_usage(message, source="chat"))
                    break

                # Keep the loop responsive — let other tasks run between
                # yields so we don't starve the heartbeat.
                await asyncio.sleep(0)

    except asyncio.CancelledError:
        logger.info("stream cancelled by client")
        raise
    except Exception as e:
        logger.exception("astra run_query failed")
        yield error(f"astra error: {e}")

    meta: dict[str, object] = {}
    final_result_obj = locals().get("final_result")
    if final_result_obj is not None:
        cost = getattr(final_result_obj, "total_cost_usd", None)
        if cost is not None:
            meta["cost_usd"] = float(cost)
        usage = getattr(final_result_obj, "usage", None)
        if isinstance(usage, dict):
            meta["input_tokens"] = int(usage.get("input_tokens") or 0)
            meta["output_tokens"] = int(usage.get("output_tokens") or 0)
    yield done(duration_ms=int((time.monotonic() - started) * 1000), meta=meta)
