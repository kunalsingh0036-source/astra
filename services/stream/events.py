"""
SSE event formatting.

Each helper writes a single server-sent event in the text/event-stream
wire format:

    event: <name>
    data: <JSON payload>
    <blank line>

Events are consumed in the browser by `new EventSource("/api/chat")`
which fires a named handler for each `event:` line.

Keep the event names short, stable, and UI-shaped: they map 1:1 to
what the user sees on screen.
"""

from __future__ import annotations

import json
from typing import Any


def _format(event: str, data: dict[str, Any]) -> bytes:
    """Encode a single SSE frame. UTF-8 bytes ready to yield."""
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {body}\n\n".encode("utf-8")


def session(session_id: str) -> bytes:
    """Sent once at the very start so the client can display it in the UI."""
    return _format("session", {"session_id": session_id})


def thought(text: str) -> bytes:
    """
    Transient reasoning string.

    These appear as italic-serif lines mid-canvas, fading in and out.
    Emitted before and during tool use so the user sees progress.
    """
    return _format("thought", {"text": text})


def tool_call(id: str, name: str, agent: str | None = None) -> bytes:
    """
    A tool invocation has started.

    On the canvas, this lights up the orb for the corresponding agent
    and draws a connection line to the "you" point.
    """
    return _format("tool_call", {"id": id, "name": name, "agent": agent})


def tool_result(id: str, preview: str, is_error: bool = False) -> bytes:
    """A tool returned. Fade the connection line when the last tool completes."""
    return _format("tool_result", {"id": id, "preview": preview, "is_error": is_error})


def text_delta(content: str) -> bytes:
    """Incremental text from the assistant. Append to the response pane."""
    return _format("text_delta", {"content": content})


def artifact(
    type: str,
    title: str | None = None,
    content: Any = None,
) -> bytes:
    """
    A structured artifact — table, chart, draft, etc.

    The browser renders these with dedicated React components inside
    the response pane.
    """
    return _format(
        "artifact",
        {"type": type, "title": title, "content": content},
    )


def done(duration_ms: int, meta: dict[str, Any] | None = None) -> bytes:
    """Final event — the turn is complete."""
    payload = {"duration_ms": duration_ms}
    if meta:
        payload.update(meta)
    return _format("done", payload)


def error(message: str) -> bytes:
    """Something went wrong mid-stream. The client shows the alarm state."""
    return _format("error", {"message": message})


def heartbeat() -> bytes:
    """
    Keep-alive comment frame.

    Sent every ~15s so long-idle proxies don't drop the connection
    during slow tool calls. Comments are ignored by EventSource.
    """
    return b": heartbeat\n\n"
