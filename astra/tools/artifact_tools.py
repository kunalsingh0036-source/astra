"""
MCP tools for emitting live artifacts to the web UI.

Artifacts are structured payloads Astra surfaces alongside its text
response. The web UI renders them with dedicated React components —
tables you can sort, drafts you can edit, metric cards you can scan.

The tool does not return user-visible text. Instead it emits a
machine-readable sentinel that the lean runtime's agent loop picks
up and emits as an `artifact` event into the turn_events log.

Astra should call these tools whenever a structured response would
be more useful than prose: lists of emails/invoices/contacts,
drafted messages, named metrics. Always still summarize in one line
of prose afterwards so the response feels natural.
"""

from __future__ import annotations

import json
from typing import Any

from astra.runtime.preview_store import create_preview
from astra.runtime.sdk_compat import tool, create_sdk_mcp_server

# Sentinel used by astra/runtime/agent_loop.py to split artifact
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
    "emit_palette",
    "Show a color palette as visual swatches in the UI. Use when "
    "presenting a color scheme — brand colors, design references, "
    "mood-board hex codes, accent palettes. Each color renders as a "
    "tappable swatch with hex + label; the user can copy a hex with "
    "one click. Strongly preferred over dumping hex codes as prose "
    "(`#0A0A0A #1A1A1A`) — that's unreadable and loses the visual.",
    {
        "name": str,  # palette name (e.g. "Film Noir", "Brand Primary")
        "colors": list,  # list[{hex: "#RRGGBB", label: "deep black"}]
        "notes": str,  # optional one-line vibe/usage note
    },
)
async def emit_palette_tool(args: dict) -> dict:
    # Defensive normalization: model sometimes returns nested dicts as
    # JSON strings, occasionally omits hex prefixes. We sanitize here
    # so the UI gets a predictable shape.
    raw_colors = args.get("colors") or []
    colors: list[dict[str, str]] = []
    for c in raw_colors:
        if not isinstance(c, dict):
            continue
        hex_value = str(c.get("hex") or c.get("color") or "").strip()
        if hex_value and not hex_value.startswith("#"):
            hex_value = f"#{hex_value}"
        label = str(c.get("label") or c.get("name") or "").strip()
        if hex_value:
            colors.append({"hex": hex_value, "label": label})
    return _emit({
        "type": "palette",
        "name": args.get("name") or "",
        "colors": colors,
        "notes": args.get("notes") or "",
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


@tool(
    "prepare_preview",
    "Save renderable content (HTML, markdown, JSON, plain text) and "
    "emit a preview artifact the user can view inline OR open in a "
    "new tab. Use when prose can't convey the result — design "
    "mockups, rendered HTML, generated SVGs, formatted reports. "
    "Stored same-origin so the iframe sandbox can render it without "
    "cross-origin issues. Default TTL is 7 days.\n\n"
    "Two modes:\n"
    "  - inline content: pass `content` (the HTML/text/etc body)\n"
    "  - remote URL: pass `url` (no DB write; preview opens the "
    "URL directly in a new tab — no inline iframe due to most sites' "
    "X-Frame-Options).",
    {
        "title": str,  # human label
        "content": str,  # body for inline mode (mutually exclusive with url)
        "content_type": str,  # MIME type (defaults to text/html; charset=utf-8)
        "url": str,  # for remote-URL mode (mutually exclusive with content)
        "notes": str,  # optional one-line caption
    },
)
async def prepare_preview_tool(args: dict) -> dict:
    title = (args.get("title") or "").strip()
    content = args.get("content") or ""
    content_type = (args.get("content_type") or "").strip()
    url = (args.get("url") or "").strip()
    notes = (args.get("notes") or "").strip()
    if url and content:
        return {
            "content": [
                {
                    "type": "text",
                    "text": "prepare_preview: pass either `content` OR `url`, not both",
                }
            ]
        }
    if not url and not content:
        return {
            "content": [
                {
                    "type": "text",
                    "text": "prepare_preview: need either `content` or `url`",
                }
            ]
        }
    payload: dict[str, Any] = {
        "type": "preview",
        "title": title,
        "notes": notes,
    }
    if url:
        # External URL — just emit the artifact pointing at it. No
        # DB write. The iframe can't reliably render external URLs
        # (X-Frame-Options) so the UI shows only the open-in-tab
        # button for url-mode previews.
        payload["url"] = url
        payload["mode"] = "url"
    else:
        try:
            preview_id = await create_preview(
                title=title or "Preview",
                body=content,
                content_type=content_type or "text/html; charset=utf-8",
            )
        except ValueError as e:
            return {
                "content": [
                    {"type": "text", "text": f"prepare_preview: {e}"}
                ]
            }
        payload["preview_id"] = preview_id
        payload["content_type"] = content_type or "text/html; charset=utf-8"
        payload["mode"] = "inline"
    return _emit(payload)


def create_artifact_mcp_server():
    """Expose the artifact tools as an MCP server."""
    return create_sdk_mcp_server(
        name="astra-artifacts",
        version="0.1.0",
        tools=[
            emit_table_tool,
            emit_draft_tool,
            emit_metric_tool,
            emit_palette_tool,
            prepare_preview_tool,
        ],
    )
