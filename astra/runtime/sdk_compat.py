"""
Minimal compatibility shim for the (now-dropped) claude_agent_sdk's
@tool / create_sdk_mcp_server / SdkMcpTool surface.

After the lean-runtime migration, no code at runtime depends on the
SDK's bundled CLI subprocess. But every astra/tools/*.py file uses
@tool and create_sdk_mcp_server to define their tools. Rather than
mechanically rewrite all 107 tool decorations, this shim provides the
same shape with zero subprocess dependency.

What this gives us:
  - astra/tools/*.py keeps working unchanged (just changes its import)
  - SdkMcpTool instances flow through astra.runtime.sdk_adapter just
    like before (the adapter introspects the same fields)
  - The astra package no longer needs claude-agent-sdk in its
    dependencies
  - We control the shape ourselves — if Anthropic ships a breaking
    change to the SDK, our code is unaffected

This is intentionally a tiny shim. We're not reimplementing the SDK,
just preserving the @tool / SdkMcpTool calling convention that was
already in our codebase.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, TypeVar

T = TypeVar("T")


# ── SdkMcpTool — same shape as the SDK's class ─────────────


@dataclass
class SdkMcpTool:
    """Mirror of `claude_agent_sdk.SdkMcpTool` — what @tool returns.

    The SDK adapter (astra.runtime.sdk_adapter) reads .name,
    .description, .input_schema, .handler from instances of this
    class to build registry entries. Same field set as the SDK.
    """

    name: str
    description: str
    input_schema: Any  # type-dict OR JSON Schema dict
    handler: Callable[[Any], Awaitable[dict[str, Any]]]
    annotations: Any = None


# ── @tool decorator — same shape as the SDK's ──────────────


def tool(
    name: str,
    description: str,
    input_schema: Any = None,
    annotations: Any = None,
) -> Callable[[Callable[..., Any]], SdkMcpTool]:
    """Decorator: wrap an async function as an SdkMcpTool.

    Drop-in replacement for `from claude_agent_sdk import tool`.
    The wrapped object has the same fields the SDK's class has, so
    code that introspects @tool-decorated functions (via the runtime
    SDK adapter) keeps working unchanged.
    """

    def decorator(handler: Callable[..., Any]) -> SdkMcpTool:
        return SdkMcpTool(
            name=name,
            description=description,
            input_schema=input_schema,
            handler=handler,
            annotations=annotations,
        )

    return decorator


# ── create_sdk_mcp_server — vestigial, kept for import compat ──


@dataclass
class _SdkMcpServer:
    """Lightweight stand-in for the SDK's MCP server object.

    The lean runtime doesn't run MCP servers — tools are dispatched
    directly via the registry. But astra/tools/*.py functions like
    `create_memory_mcp_server()` still call this constructor at import
    time, and we don't want them to crash. We return an inert object
    that holds the tool list; nothing reads it.
    """

    name: str
    version: str
    tools: list[SdkMcpTool] = field(default_factory=list)


def create_sdk_mcp_server(
    name: str,
    version: str = "0.1.0",
    tools: list[SdkMcpTool] | None = None,
) -> _SdkMcpServer:
    """Drop-in replacement for `claude_agent_sdk.create_sdk_mcp_server`.

    Returns an inert wrapper that the lean runtime never reads. Kept so
    the `astra/tools/*.py` files' module-level calls don't crash; the
    actual tool-dispatch path goes through the registry which already
    has these tools registered via `astra.runtime.sdk_adapter`.
    """
    return _SdkMcpServer(
        name=name, version=version, tools=list(tools or [])
    )
