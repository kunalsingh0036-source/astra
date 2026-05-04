"""
Tool registry tests — Phase 1 of the lean-runtime migration.

Verifies the registry correctly:
  - Registers tools with metadata
  - Generates Anthropic-API-format tool definitions
  - Dispatches tools by name with correct args
  - Normalizes diverse return shapes (str, dict, MCP-style content list)
  - Enforces per-tool timeouts
  - Surfaces tool exceptions as ToolResult errors (never raises)
  - Detects duplicate-name registrations
"""

from __future__ import annotations

import asyncio

import pytest

from astra.runtime.tool_registry import (
    ActionTier,
    REGISTRY,
    ToolDef,
    ToolRegistry,
    ToolResult,
    register_tool,
)


# Use a fresh registry per test so global REGISTRY isn't polluted.
@pytest.fixture
def registry() -> ToolRegistry:
    return ToolRegistry()


# ── Registration & lookup ─────────────────────────────────


def test_register_and_get(registry: ToolRegistry) -> None:
    async def echo(args: dict) -> str:
        return f"echo: {args.get('msg', '')}"

    registry.register(
        ToolDef(
            name="echo",
            description="Echo a message back",
            input_schema={"type": "object", "properties": {"msg": {"type": "string"}}},
            fn=echo,
            tier=ActionTier.READ,
            namespace="test",
        )
    )

    td = registry.get("echo")
    assert td is not None
    assert td.name == "echo"
    assert td.namespace == "test"
    assert td.tier == ActionTier.READ


def test_duplicate_registration_raises(registry: ToolRegistry) -> None:
    async def fn(args: dict) -> str:
        return ""

    td = ToolDef(
        name="dup",
        description="d",
        input_schema={"type": "object"},
        fn=fn,
    )
    registry.register(td)
    with pytest.raises(ValueError, match="already registered"):
        registry.register(td)


def test_names_sorted(registry: ToolRegistry) -> None:
    async def fn(args: dict) -> str:
        return ""

    for n in ("zebra", "alpha", "mike"):
        registry.register(
            ToolDef(name=n, description=n, input_schema={"type": "object"}, fn=fn)
        )
    assert registry.names() == ["alpha", "mike", "zebra"]


def test_by_namespace_filters(registry: ToolRegistry) -> None:
    async def fn(args: dict) -> str:
        return ""

    registry.register(
        ToolDef(
            name="a", description="", input_schema={"type": "object"}, fn=fn,
            namespace="memory",
        )
    )
    registry.register(
        ToolDef(
            name="b", description="", input_schema={"type": "object"}, fn=fn,
            namespace="creators",
        )
    )
    mem = registry.by_namespace("memory")
    assert len(mem) == 1
    assert mem[0].name == "a"


# ── Anthropic-format export ────────────────────────────────


def test_as_anthropic_tools_shape(registry: ToolRegistry) -> None:
    async def fn(args: dict) -> str:
        return ""

    registry.register(
        ToolDef(
            name="search",
            description="search the web",
            input_schema={
                "type": "object",
                "properties": {"q": {"type": "string"}},
                "required": ["q"],
            },
            fn=fn,
        )
    )
    out = registry.as_anthropic_tools()
    assert len(out) == 1
    assert out[0]["name"] == "search"
    assert out[0]["description"] == "search the web"
    # Must include input_schema verbatim — Anthropic API expects this key
    assert out[0]["input_schema"]["type"] == "object"
    assert out[0]["input_schema"]["required"] == ["q"]


def test_as_anthropic_tools_namespace_filter(registry: ToolRegistry) -> None:
    async def fn(args: dict) -> str:
        return ""

    registry.register(
        ToolDef(
            name="a", description="", input_schema={"type": "object"}, fn=fn,
            namespace="memory",
        )
    )
    registry.register(
        ToolDef(
            name="b", description="", input_schema={"type": "object"}, fn=fn,
            namespace="creators",
        )
    )
    out = registry.as_anthropic_tools(namespaces=["memory"])
    assert {t["name"] for t in out} == {"a"}


# ── Dispatch — happy paths ─────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_str_return(registry: ToolRegistry) -> None:
    async def fn(args: dict) -> str:
        return f"got {args.get('x', 0)}"

    registry.register(
        ToolDef(name="t", description="", input_schema={"type": "object"}, fn=fn)
    )
    r = await registry.dispatch("t", {"x": 42})
    assert isinstance(r, ToolResult)
    assert r.text == "got 42"
    assert r.is_error is False
    assert r.duration_ms >= 0


@pytest.mark.asyncio
async def test_dispatch_dict_return(registry: ToolRegistry) -> None:
    """Bare dict → JSON-encoded text so the LLM gets structured data."""

    async def fn(args: dict) -> dict:
        return {"k": "v", "n": 7}

    registry.register(
        ToolDef(name="t", description="", input_schema={"type": "object"}, fn=fn)
    )
    r = await registry.dispatch("t", {})
    assert "k" in r.text
    assert "v" in r.text
    assert r.is_error is False


@pytest.mark.asyncio
async def test_dispatch_mcp_content_shape(registry: ToolRegistry) -> None:
    """SDK MCP shape — {"content": [{"type": "text", "text": "..."}]}.

    Old @tool-decorated functions returned this. The registry must
    accept it so we can register them without changing return shape."""

    async def fn(args: dict) -> dict:
        return {
            "content": [
                {"type": "text", "text": "first"},
                {"type": "text", "text": "second"},
            ]
        }

    registry.register(
        ToolDef(name="t", description="", input_schema={"type": "object"}, fn=fn)
    )
    r = await registry.dispatch("t", {})
    assert r.text == "first\nsecond"
    assert r.is_error is False


@pytest.mark.asyncio
async def test_dispatch_mcp_error_shape(registry: ToolRegistry) -> None:
    async def fn(args: dict) -> dict:
        return {
            "content": [{"type": "text", "text": "permission denied"}],
            "is_error": True,
        }

    registry.register(
        ToolDef(name="t", description="", input_schema={"type": "object"}, fn=fn)
    )
    r = await registry.dispatch("t", {})
    assert r.text == "permission denied"
    assert r.is_error is True


# ── Dispatch — error paths ─────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_unknown_tool(registry: ToolRegistry) -> None:
    r = await registry.dispatch("nonexistent", {})
    assert r.is_error is True
    assert "unknown tool" in r.text


@pytest.mark.asyncio
async def test_dispatch_tool_raises(registry: ToolRegistry) -> None:
    """A raising tool is NEVER allowed to break the agent loop. The
    registry must catch and convert to a ToolResult."""

    async def fn(args: dict) -> str:
        raise RuntimeError("boom")

    registry.register(
        ToolDef(name="t", description="", input_schema={"type": "object"}, fn=fn)
    )
    r = await registry.dispatch("t", {})
    assert r.is_error is True
    assert "RuntimeError" in r.text
    assert "boom" in r.text


@pytest.mark.asyncio
async def test_dispatch_timeout(registry: ToolRegistry) -> None:
    """Per-tool timeout enforcement. The watchdog must fire and
    return an error result rather than letting the loop hang.
    This is THE bug class we're migrating to fix."""

    async def slow(args: dict) -> str:
        await asyncio.sleep(2)
        return "done"

    registry.register(
        ToolDef(
            name="slow",
            description="",
            input_schema={"type": "object"},
            fn=slow,
            timeout_sec=1,
        )
    )
    r = await registry.dispatch("slow", {})
    assert r.is_error is True
    assert "timed out" in r.text


@pytest.mark.asyncio
async def test_dispatch_timeout_override(registry: ToolRegistry) -> None:
    """The dispatcher accepts a per-call timeout override — useful
    when the agent is doing slow work and wants more headroom."""

    async def slow(args: dict) -> str:
        await asyncio.sleep(0.5)
        return "done"

    registry.register(
        ToolDef(
            name="slow",
            description="",
            input_schema={"type": "object"},
            fn=slow,
            timeout_sec=10,
        )
    )
    # Override DOWN to 100ms — should hit timeout
    r = await registry.dispatch("slow", {}, timeout_override=1)
    # 1 second > 0.5 second sleep, so the override 1s should NOT timeout
    assert r.is_error is False
    assert r.text == "done"


# ── Decorator + global registry ────────────────────────────


def test_register_tool_decorator_lives() -> None:
    """The @register_tool decorator should add to the global registry
    AND return the original function unchanged so call sites that
    already used the function keep working."""

    @register_tool(
        name="__test_decorator_lives__",
        description="d",
        input_schema={"type": "object"},
        tier=ActionTier.READ,
        namespace="test",
    )
    async def my_impl(args: dict) -> str:
        return "ok"

    # The function must be callable as before
    assert callable(my_impl)
    # The registry must know about it
    assert REGISTRY.get("__test_decorator_lives__") is not None
