"""
Tool registry — runtime-independent dispatch layer.

The Claude Agent SDK couples three things into one decorator:
  1. The tool's Python implementation
  2. Its name + description + input schema
  3. The MCP transport that exposes it to the bundled CLI subprocess

That coupling means tools are only usable through the SDK CLI — and
when the CLI hangs (which it does), every tool we own becomes
unreachable. Unwinding this is the foundation of the lean runtime
migration.

This registry decouples the three:
  - You define a tool ONCE here with @register_tool(...).
  - The SDK adapter (existing astra.tools.*) keeps wrapping these for
    backwards compatibility — the bundled CLI keeps working.
  - The lean runtime (future astra.runtime.agent_loop) calls
    REGISTRY.dispatch(name, args) directly.
  - The Anthropic Messages API tool definition is generated from the
    same metadata via REGISTRY.as_anthropic_tools().

One tool, two callers, no forks.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


class ActionTier(enum.Enum):
    """Same tiering as astra.autonomy.modes.ActionTier — mirrored here
    so the registry doesn't depend on the autonomy module (avoids a
    circular import once the lean runtime calls into autonomy itself).
    The autonomy module is the source of truth for permission rules;
    this is just a label."""

    READ = "read"
    WRITE = "write"
    DESTRUCTIVE = "destructive"


# A tool implementation receives the parsed args dict and returns a
# string, dict, or a list of content blocks (Anthropic format). The
# registry normalizes these to a single ToolResult on dispatch.
ToolImpl = Callable[[dict[str, Any]], Awaitable[Any]]


@dataclass
class ToolResult:
    """Normalized output of any tool dispatch.

    `text` is what the LLM sees in the tool_result content. `is_error`
    flips the response role (Anthropic's protocol: tool_result with
    is_error=True means "tool tried but failed", distinct from a
    successful tool returning bad data).
    """

    text: str
    is_error: bool = False
    duration_ms: int = 0
    raw: Any = None  # original return value, for debugging/audit


@dataclass
class ToolDef:
    name: str
    description: str
    # JSON Schema for the tool's input. Anthropic Messages API expects
    # `input_schema` per tool — we pass this through verbatim.
    input_schema: dict[str, Any]
    fn: ToolImpl
    tier: ActionTier = ActionTier.WRITE
    # Per-tool timeout in seconds. The bundled SDK CLI had opaque
    # internal timeouts that nobody could see or tune. Here every tool
    # declares its own — fast lookups can be 5s, slow LLM-adjacent
    # tools can be 60s. Watchdog enforces this in dispatch().
    timeout_sec: int = 30
    # If True, the tool can be called concurrently with itself (no
    # shared state). Most tools are idempotent reads and qualify.
    concurrent_safe: bool = True
    # Source module for grouping in MCP servers (e.g. "memory",
    # "creators", "calendar"). Used by the SDK adapter to decide
    # which MCP server should expose this tool.
    namespace: str = "general"

    def to_anthropic_tool(self) -> dict[str, Any]:
        """Render this tool in the format the Anthropic Messages API
        expects in `tools=[...]`. The lean runtime calls this for every
        registered tool to assemble the per-turn tool list."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


class ToolRegistry:
    """In-process registry of all Astra tools."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolDef] = {}

    # ── Registration ────────────────────────────────────────

    def register(self, tool_def: ToolDef) -> None:
        if tool_def.name in self._tools:
            # Re-registration is a programming error — duplicate names
            # would silently shadow each other. Make it loud.
            raise ValueError(
                f"tool {tool_def.name!r} is already registered "
                f"(in namespace {self._tools[tool_def.name].namespace!r})"
            )
        self._tools[tool_def.name] = tool_def
        logger.info(
            "[registry] registered %s (namespace=%s, tier=%s, timeout=%ds)",
            tool_def.name,
            tool_def.namespace,
            tool_def.tier.value,
            tool_def.timeout_sec,
        )

    # ── Lookup ─────────────────────────────────────────────

    def get(self, name: str) -> ToolDef | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools.keys())

    def by_namespace(self, namespace: str) -> list[ToolDef]:
        return [t for t in self._tools.values() if t.namespace == namespace]

    def all(self) -> list[ToolDef]:
        return list(self._tools.values())

    # ── Anthropic-format export ────────────────────────────

    def as_anthropic_tools(
        self, *, namespaces: list[str] | None = None
    ) -> list[dict[str, Any]]:
        """Return the tool list in the shape Anthropic Messages API
        wants. Optionally filter to a subset of namespaces — useful
        if a particular agent flow only needs memory tools, not
        creators tools, etc."""
        items = self.all()
        if namespaces is not None:
            items = [t for t in items if t.namespace in namespaces]
        return [t.to_anthropic_tool() for t in items]

    # ── Dispatch ────────────────────────────────────────────

    async def dispatch(
        self,
        name: str,
        args: dict[str, Any] | None = None,
        *,
        timeout_override: int | None = None,
    ) -> ToolResult:
        """Run a tool by name, with timeout enforcement and result
        normalization. NEVER raises — all failures come back as a
        ToolResult with is_error=True, so the agent loop can keep
        going (the LLM sees the error, decides what to do)."""
        td = self.get(name)
        if td is None:
            return ToolResult(
                text=f"unknown tool: {name!r}. registered tools: {self.names()}",
                is_error=True,
            )

        timeout = timeout_override or td.timeout_sec
        args = dict(args or {})
        started = time.monotonic()

        try:
            raw = await asyncio.wait_for(td.fn(args), timeout=timeout)
        except asyncio.TimeoutError:
            elapsed = int((time.monotonic() - started) * 1000)
            logger.warning(
                "[registry] tool %s timed out after %dms (limit %ds)",
                name,
                elapsed,
                timeout,
            )
            return ToolResult(
                text=f"tool {name} timed out after {timeout}s",
                is_error=True,
                duration_ms=elapsed,
            )
        except Exception as e:
            elapsed = int((time.monotonic() - started) * 1000)
            logger.exception("[registry] tool %s raised", name)
            return ToolResult(
                text=f"tool {name} raised {type(e).__name__}: {e}",
                is_error=True,
                duration_ms=elapsed,
            )

        elapsed = int((time.monotonic() - started) * 1000)
        return _normalize(raw, duration_ms=elapsed)


def _normalize(raw: Any, *, duration_ms: int) -> ToolResult:
    """Normalize whatever a tool returned into a ToolResult.

    Tools historically returned different shapes depending on whether
    they were SDK MCP wrappers (which return {"content": [{"type":
    "text", "text": "..."}]}) or simple functions (which return a
    string or dict). The registry accepts all three — tools don't
    have to know the API surface.
    """
    if isinstance(raw, str):
        return ToolResult(text=raw, raw=raw, duration_ms=duration_ms)
    if isinstance(raw, dict):
        # SDK MCP shape: {"content": [...], "is_error": bool}
        if "content" in raw and isinstance(raw["content"], list):
            chunks: list[str] = []
            for block in raw["content"]:
                if isinstance(block, dict) and "text" in block:
                    chunks.append(str(block["text"]))
                elif isinstance(block, str):
                    chunks.append(block)
            return ToolResult(
                text="\n".join(chunks),
                is_error=bool(raw.get("is_error", False)),
                raw=raw,
                duration_ms=duration_ms,
            )
        # Bare dict — render as JSON so the LLM gets structured data.
        import json
        return ToolResult(
            text=json.dumps(raw, indent=2, default=str),
            raw=raw,
            duration_ms=duration_ms,
        )
    # Anything else — stringify defensively.
    return ToolResult(text=str(raw), raw=raw, duration_ms=duration_ms)


# ── Module-level singleton + decorator ─────────────────────

REGISTRY = ToolRegistry()


def register_tool(
    *,
    name: str,
    description: str,
    input_schema: dict[str, Any] | None = None,
    tier: ActionTier = ActionTier.WRITE,
    timeout_sec: int = 30,
    namespace: str = "general",
    concurrent_safe: bool = True,
) -> Callable[[ToolImpl], ToolImpl]:
    """Decorator: register a tool in the global registry.

    Mirrors the shape of the SDK's @tool decorator so porting an
    existing tool is a one-line swap (plus adding `tier`, `namespace`,
    `timeout_sec` which the SDK didn't track explicitly).

    Example:
        @register_tool(
            name="recall_recent_turns",
            description="Pull the most recent chat turns from the turns table.",
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer"},
                    "session_id": {"type": "string"},
                },
            },
            tier=ActionTier.READ,
            timeout_sec=10,
            namespace="memory",
        )
        async def recall_recent_turns_impl(args: dict) -> str:
            ...
    """
    schema = input_schema or {"type": "object", "properties": {}}

    def decorator(fn: ToolImpl) -> ToolImpl:
        REGISTRY.register(
            ToolDef(
                name=name,
                description=description,
                input_schema=schema,
                fn=fn,
                tier=tier,
                timeout_sec=timeout_sec,
                concurrent_safe=concurrent_safe,
                namespace=namespace,
            )
        )
        return fn

    return decorator
