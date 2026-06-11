"""
Local-machine bridge tools — operate on Kunal's Mac via the bridge.

When Astra (running on Railway) calls one of these tools:
  1. We resolve the active bridge_token_id (the one Mac daemon
     currently online — or None if nobody's connected).
  2. We queue a row in bridge_calls with the tool_name + args.
  3. We wait (with timeout) for the daemon to pick it up, run the
     action locally, and POST the result back.
  4. We return the result text to the agent.

If no daemon is online we return immediately with a clear error so
the model doesn't hang for 30s. Same for timeouts — every tool has
a tight wait_for_result cap.

Path allowlist: server-side double-check via is_path_allowed so a
prompt-injection trying to read /etc/passwd gets rejected before it
even leaves Railway. The daemon does its own check too.
"""

from __future__ import annotations

import json
import logging
import os

from astra.runtime.bridge.store import (
    is_path_allowed,
    queue_call,
    wait_for_result,
)
from astra.runtime.tool_registry import ActionTier, register_tool

logger = logging.getLogger(__name__)


# ── Bridge resolution ─────────────────────────────────────


async def _active_bridge_token_id() -> tuple[int | None, list[str]]:
    """Return the (id, allowed_paths) of the most-recently-active
    bridge token. We pick the one with the most recent last_seen_at
    within the last 60 seconds — this is "the daemon currently
    online and polling."

    For v1 we assume one bridge per Astra instance. Multi-bridge
    routing (e.g. "execute on the studio mac vs the laptop") would
    take a token-name argument here.
    """
    try:
        from sqlalchemy import text
        from astra.db.engine import async_session
    except Exception:
        return None, []

    try:
        async with async_session() as s:
            r = await s.execute(
                text(
                    """
                    SELECT id, allowed_paths
                    FROM bridge_tokens
                    WHERE revoked_at IS NULL
                      AND last_seen_at IS NOT NULL
                      AND last_seen_at > now() - INTERVAL '60 seconds'
                    ORDER BY last_seen_at DESC
                    LIMIT 1
                    """
                )
            )
            row = r.first()
    except Exception:
        return None, []

    if not row:
        return None, []
    paths = row[1] or []
    if isinstance(paths, str):
        try:
            paths = json.loads(paths)
        except json.JSONDecodeError:
            paths = []
    return int(row[0]), list(paths)


async def _dispatch(
    tool_name: str, args: dict, *, timeout_sec: float
) -> dict:
    """Common path: resolve bridge → queue call → wait → return."""
    token_id, allowed_paths = await _active_bridge_token_id()
    if token_id is None:
        return {
            "content": [
                {
                    "type": "text",
                    "text": (
                        "no local bridge daemon is currently online. "
                        "kunal needs to start the bridge: "
                        "`python -m astra.bridge_daemon --token $ASTRA_BRIDGE_TOKEN`. "
                        "until then, local_* tools are unavailable."
                    ),
                }
            ],
            "is_error": True,
        }

    # Defensive path check on the server side. Daemon does its own,
    # but rejecting here saves a network round-trip + a daemon-side
    # error log for obvious traversal attempts.
    path = args.get("path") or args.get("cwd")
    if path and tool_name != "local_bash" and not is_path_allowed(path, allowed_paths):
        return {
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"path {path!r} is not in the bridge's allowlist. "
                        f"allowed roots: {allowed_paths}"
                    ),
                }
            ],
            "is_error": True,
        }

    # local_glob takes `pattern` not `path` — verify the pattern's
    # static prefix lands inside an allowed root. Without this, the
    # daemon would still reject (its own check matches), but pre-
    # rejecting here saves a round-trip and prevents a stuck pending
    # row from a malformed pattern.
    if tool_name == "local_glob":
        pattern = args.get("pattern", "")
        if pattern and not any(
            pattern == r.rstrip("/") or pattern.startswith(r.rstrip("/") + "/")
            for r in allowed_paths
        ):
            return {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"glob pattern must start under an allowed root. "
                            f"pattern={pattern!r} allowed_roots={allowed_paths}"
                        ),
                    }
                ],
                "is_error": True,
            }

    call_id = await queue_call(
        bridge_token_id=token_id,
        tool_name=tool_name,
        args=args,
    )
    result = await wait_for_result(call_id, timeout_sec=timeout_sec)

    if result.status == "timeout":
        return {
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"local bridge timed out after {timeout_sec}s. "
                        "the daemon may be stuck or the action took too long."
                    ),
                }
            ],
            "is_error": True,
        }
    if result.status == "failed":
        return {
            "content": [
                {
                    "type": "text",
                    "text": result.error_message or "local bridge failed (no message)",
                }
            ],
            "is_error": True,
        }
    return {
        "content": [{"type": "text", "text": result.result or ""}],
        "is_error": False,
    }


# ── Tools ────────────────────────────────────────────────


@register_tool(
    name="local_read",
    description=(
        "Read a file on Kunal's Mac via the local bridge. Returns up to "
        "`limit` lines starting at `offset` (1-indexed). Use this when "
        "the user references a file path — code, document, log — that "
        "lives on the Mac, not on the server. The bridge daemon enforces "
        "an allowlist of root directories; outside-of-allowlist paths "
        "are rejected. Returns an error if no daemon is online."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute path to read on the Mac",
            },
            "offset": {
                "type": "integer",
                "description": "1-indexed line to start at (default 1)",
            },
            "limit": {
                "type": "integer",
                "description": "Max lines to return (default 200, max 2000)",
            },
        },
        "required": ["path"],
    },
    tier=ActionTier.READ,
    timeout_sec=40,
    namespace="local",
)
async def local_read_impl(args: dict) -> dict:
    return await _dispatch("local_read", args, timeout_sec=20.0)


@register_tool(
    name="local_write",
    description=(
        "Write a file on Kunal's Mac via the local bridge. OVERWRITES "
        "the target. Creates parent directories as needed. Use sparingly — "
        "for new files, prefer asking before write; for edits, prefer "
        "local_edit which preserves surrounding content. Path must be in "
        "the bridge's allowlist."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["path", "content"],
    },
    tier=ActionTier.WRITE,
    timeout_sec=35,
    namespace="local",
)
async def local_write_impl(args: dict) -> dict:
    return await _dispatch("local_write", args, timeout_sec=15.0)


@register_tool(
    name="local_edit",
    description=(
        "Edit a file on Kunal's Mac by replacing a string. The `old_string` "
        "must match exactly once in the file (more than one match is an "
        "error — provide more context). Preserves everything else. Use "
        "for surgical changes; use local_write only when the whole file "
        "is being replaced."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old_string": {
                "type": "string",
                "description": "Exact text to find. Must occur exactly once.",
            },
            "new_string": {
                "type": "string",
                "description": "Replacement text.",
            },
        },
        "required": ["path", "old_string", "new_string"],
    },
    tier=ActionTier.WRITE,
    timeout_sec=35,
    namespace="local",
)
async def local_edit_impl(args: dict) -> dict:
    return await _dispatch("local_edit", args, timeout_sec=15.0)


@register_tool(
    name="local_bash",
    description=(
        "Run a shell command on Kunal's Mac. DESTRUCTIVE — autonomy "
        "gate applies. Output is captured (stdout + stderr); the tool "
        "returns the combined stream plus exit_code. Use for git, "
        "build commands, scripts, anything that needs the local "
        "shell. The daemon may enforce a command pattern allowlist on "
        "top of the autonomy tier."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Shell command to execute.",
            },
            "cwd": {
                "type": "string",
                "description": (
                    "Working directory (must be in allowlist). "
                    "Defaults to the daemon's home dir."
                ),
            },
            "timeout_sec": {
                "type": "integer",
                "description": "Daemon-side execution timeout (default 30, max 120)",
            },
        },
        "required": ["command"],
    },
    tier=ActionTier.DESTRUCTIVE,
    timeout_sec=160,
    namespace="local",
)
async def local_bash_impl(args: dict) -> dict:
    return await _dispatch("local_bash", args, timeout_sec=140.0)


@register_tool(
    name="local_glob",
    description=(
        "Find files matching a glob pattern on Kunal's Mac. Returns up "
        "to 500 paths. Use when the user describes a file by name "
        "without giving an exact path."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Glob (e.g. '/Users/kunalsingh/Documents/**/*.md')",
            },
        },
        "required": ["pattern"],
    },
    tier=ActionTier.READ,
    timeout_sec=40,
    namespace="local",
)
async def local_glob_impl(args: dict) -> dict:
    return await _dispatch("local_glob", args, timeout_sec=20.0)


@register_tool(
    name="local_grep",
    description=(
        "Search for a regex pattern across files on Kunal's Mac. "
        "Returns up to 200 matches with file path, line number, and "
        "the matching line. Path must be in the bridge's allowlist."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Python-style regex.",
            },
            "path": {
                "type": "string",
                "description": "Directory to search under.",
            },
            "include": {
                "type": "string",
                "description": "Optional file glob filter (e.g. '*.py').",
            },
        },
        "required": ["pattern", "path"],
    },
    tier=ActionTier.READ,
    timeout_sec=50,
    namespace="local",
)
async def local_grep_impl(args: dict) -> dict:
    return await _dispatch("local_grep", args, timeout_sec=30.0)


@register_tool(
    name="local_bridge_status",
    description=(
        "Check whether a local bridge daemon is online and what it's "
        "allowed to access. Use BEFORE attempting local_* tools when "
        "uncertain whether the bridge is reachable, or when the user "
        "asks 'what can you see on my mac'."
    ),
    input_schema={"type": "object", "properties": {}},
    tier=ActionTier.READ,
    timeout_sec=5,
    namespace="local",
)
async def local_bridge_status_impl(args: dict) -> dict:
    token_id, allowed_paths = await _active_bridge_token_id()
    if token_id is None:
        return {
            "content": [
                {
                    "type": "text",
                    "text": (
                        "No local bridge daemon is online. The Mac side isn't "
                        "running `python -m astra.bridge_daemon` right now."
                    ),
                }
            ]
        }
    return {
        "content": [
            {
                "type": "text",
                "text": (
                    f"Local bridge online (token #{token_id}). "
                    f"Allowed roots ({len(allowed_paths)}):\n"
                    + "\n".join(f"  - {p}" for p in allowed_paths)
                ),
            }
        ]
    }


@register_tool(
    name="screenshot_url",
    description=(
        "Capture a remote URL as a PNG screenshot via the local bridge "
        "daemon (headless Chrome on Kunal's Mac), then emit an image "
        "artifact the user can see inline + open full-size in a tab. "
        "Use when prose can't carry the visual: 'show me what 375.studio "
        "looks like', 'compare the homepages of these three sites'. "
        "Returns shrugs if the bridge isn't online — no fallback to a "
        "remote service. Viewport defaults to 1440×900; can be widened "
        "for desktop or narrowed for mobile."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "https:// or http:// URL to capture",
            },
            "viewport_width": {
                "type": "integer",
                "description": "px, default 1440 (desktop), 390 for iPhone-sized",
            },
            "viewport_height": {
                "type": "integer",
                "description": "px, default 900",
            },
            "title": {
                "type": "string",
                "description": "human label for the artifact (e.g. '375.studio homepage')",
            },
            "notes": {
                "type": "string",
                "description": "optional one-line caption shown below the image",
            },
        },
        "required": ["url"],
    },
    tier=ActionTier.READ,
    # Outer cap. Inner: chrome subprocess timeout (30s) + bridge
    # network + dispatch overhead. 60s leaves margin without hanging
    # the runner per-turn budget.
    timeout_sec=75,
    namespace="local",
)
async def screenshot_url_impl(args: dict) -> dict:
    """Bridge-side capture → wrap as an image artifact via the sentinel.

    The daemon returns a JSON blob with {url, width, height, byte_count,
    png_base64}. We turn that into a `data:image/png;base64,…` URI
    embedded in an `image` artifact. Same wire format as the other
    artifact-emitting tools (table/draft/metric/palette/preview).
    """
    import json as _json

    # Reuse the existing bridge dispatch — same auth, timeout
    # plumbing, error reporting.
    bridge_args = {
        "url": (args.get("url") or "").strip(),
        "viewport_width": args.get("viewport_width") or 1440,
        "viewport_height": args.get("viewport_height") or 900,
    }
    raw = await _dispatch(
        "local_screenshot", bridge_args, timeout_sec=55.0
    )
    if raw.get("is_error"):
        # Pass the bridge's error through unchanged so the agent
        # gets a real reason it can react to.
        return raw
    text_content = ""
    for b in raw.get("content", []):
        if isinstance(b, dict) and b.get("type") == "text":
            text_content += str(b.get("text") or "")
    if not text_content:
        return {
            "content": [
                {"type": "text", "text": "screenshot_url: empty bridge response"}
            ],
            "is_error": True,
        }
    try:
        payload = _json.loads(text_content)
    except _json.JSONDecodeError:
        # The bridge sometimes returns a plain error string when
        # something pre-screenshot fails (no chrome installed, bad
        # URL). Surface it.
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"screenshot_url failed: {text_content[:500]}",
                }
            ],
            "is_error": True,
        }
    encoded = payload.get("png_base64") or ""
    if not encoded:
        return {
            "content": [
                {"type": "text", "text": "screenshot_url: bridge returned no PNG bytes"}
            ],
            "is_error": True,
        }
    data_url = f"data:image/png;base64,{encoded}"
    artifact_payload = {
        "type": "image",
        "title": (args.get("title") or "").strip()
        or _default_image_title(payload.get("url") or ""),
        "url": data_url,
        "alt": (args.get("title") or "").strip() or payload.get("url") or "",
        "notes": (args.get("notes") or "").strip(),
        "source_url": payload.get("url") or "",
        "width": payload.get("width"),
        "height": payload.get("height"),
        "byte_count": payload.get("byte_count"),
    }
    sentinel = (
        f"⟦ASTRA_ARTIFACT⟧"
        f"{_json.dumps(artifact_payload, ensure_ascii=False, separators=(',', ':'))}"
        f"⟦/ASTRA_ARTIFACT⟧"
    )
    return {"content": [{"type": "text", "text": sentinel}]}


def _default_image_title(url: str) -> str:
    """Compact title from a URL when the agent didn't provide one."""
    if not url:
        return "screenshot"
    from urllib.parse import urlparse

    try:
        host = urlparse(url).netloc or url
    except Exception:
        host = url
    return f"screenshot · {host}"
