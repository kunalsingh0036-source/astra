"""
MCP tools for emitting live artifacts to the web UI.

Artifacts are structured payloads Astra surfaces alongside its text
response. The web UI renders them with dedicated React components —
tables you can sort, drafts you can edit, metric cards you can scan.

The tool does not return user-visible text. Instead it emits a
machine-readable sentinel that the astra-stream runner picks up and
forwards as an SSE `artifact` event.

Astra should call these tools whenever a structured response would
be more useful than prose: lists of emails/invoices/contacts,
drafted messages, named metrics. Always still summarize in one line
of prose afterwards so the response feels natural.
"""

from __future__ import annotations

import json
from typing import Any

from astra.runtime.sdk_compat import tool, create_sdk_mcp_server

# Sentinel used by astra-stream/stream/runner.py to split artifact
# payloads out of the text stream. Kept short and unlikely to appear
# in natural prose.
ARTIFACT_SENTINEL_OPEN = "⟦ASTRA_ARTIFACT⟧"
ARTIFACT_SENTINEL_CLOSE = "⟦/ASTRA_ARTIFACT⟧"


def _emit(payload: dict[str, Any]) -> dict[str, Any]:
    """Serialize an artifact payload into a tool response the runner can find."""
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    text = f"{ARTIFACT_SENTINEL_OPEN}{body}{ARTIFACT_SENTINEL_CLOSE}"
    # Response text is not shown to the user — the runner strips it.
    return {"content": [{"type": "text", "text": text}]}


@tool(
    "emit_table",
    "Show a live table in the UI alongside your response. Use when you "
    "have tabular data: a list of emails, invoices, contacts, tasks, "
    "etc. Columns should be short labels; rows are arrays of cells. "
    "The user can sort by clicking column headers.",
    {
        "title": str,
        "columns": list,  # list[str] — column labels
        "rows": list,  # list[list[str | int | float]]
        "caption": str,  # optional one-line caption below the table
    },
)
async def emit_table_tool(args: dict) -> dict:
    return _emit({
        "type": "table",
        "title": args.get("title") or "",
        "columns": args.get("columns") or [],
        "rows": args.get("rows") or [],
        "caption": args.get("caption") or "",
    })


@tool(
    "emit_draft",
    "Show a draft message in the UI that the user can review and edit. "
    "Use when you've composed an email, reply, or message and want the "
    "user to send it. The user can edit inline, approve, or discard.",
    {
        "to": str,  # comma-separated recipients
        "cc": str,  # optional
        "subject": str,  # optional for non-email drafts
        "body": str,
        "channel": str,  # "email" | "whatsapp" | "linkedin" | "slack"
    },
)
async def emit_draft_tool(args: dict) -> dict:
    return _emit({
        "type": "draft",
        "to": args.get("to") or "",
        "cc": args.get("cc") or "",
        "subject": args.get("subject") or "",
        "body": args.get("body") or "",
        "channel": args.get("channel") or "email",
    })


@tool(
    "emit_metric",
    "Show a single headline metric with label, value, and optional "
    "sub-text. Use for standalone numbers worth highlighting: cash "
    "balance, unread count, response time. Keep the value short — "
    "this is a scannable tile.",
    {
        "label": str,
        "value": str,  # pre-formatted (e.g. '₹177k', '86 unread')
        "sub": str,  # optional qualifier below
        "tone": str,  # "default" | "urgent"
    },
)
async def emit_metric_tool(args: dict) -> dict:
    return _emit({
        "type": "metric",
        "label": args.get("label") or "",
        "value": args.get("value") or "",
        "sub": args.get("sub") or "",
        "tone": args.get("tone") or "default",
    })


def create_artifact_mcp_server():
    """Expose the artifact tools as an MCP server."""
    return create_sdk_mcp_server(
        name="astra-artifacts",
        version="0.1.0",
        tools=[emit_table_tool, emit_draft_tool, emit_metric_tool],
    )
