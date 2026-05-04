"""
SDK → Registry adapter tests.

Validates that introspection of SdkMcpTool-shaped objects produces
correct registry entries — schema conversion, tier inference, timeout
heuristics, namespace assignment, idempotency.
"""

from __future__ import annotations

import pytest

from astra.runtime.sdk_adapter import (
    _is_json_schema,
    _to_json_schema,
    _guess_tier,
    _guess_timeout,
    import_sdk_tools,
)
from astra.runtime.tool_registry import ActionTier, ToolRegistry


class _FakeSdkTool:
    """Mirror of claude_agent_sdk.SdkMcpTool's public attributes."""

    def __init__(self, name, description, input_schema, handler):
        self.name = name
        self.description = description
        self.input_schema = input_schema
        self.handler = handler


# ── Schema conversion ────────────────────────────────────


def test_is_json_schema_detects_object_type() -> None:
    assert _is_json_schema({"type": "object", "properties": {}}) is True
    assert _is_json_schema({"type": "string"}) is True
    assert _is_json_schema({"name": str}) is False
    assert _is_json_schema({}) is False
    assert _is_json_schema(None) is False


def test_to_json_schema_python_types() -> None:
    """SDK shorthand {"name": str, "count": int} → JSON Schema."""
    out = _to_json_schema({"name": str, "count": int, "ratio": float, "ok": bool})
    assert out["type"] == "object"
    assert out["properties"]["name"] == {"type": "string"}
    assert out["properties"]["count"] == {"type": "integer"}
    assert out["properties"]["ratio"] == {"type": "number"}
    assert out["properties"]["ok"] == {"type": "boolean"}


def test_to_json_schema_passthrough() -> None:
    """An already-JSON-Schema dict goes through unchanged."""
    schema = {
        "type": "object",
        "properties": {"q": {"type": "string"}},
        "required": ["q"],
    }
    out = _to_json_schema(schema)
    assert out is schema or out == schema


def test_to_json_schema_unknown_type_falls_back_to_string() -> None:
    """A python type we don't recognize gets mapped to 'string' so the
    schema is at least valid for the Anthropic API."""

    class CustomType:
        pass

    out = _to_json_schema({"x": CustomType})
    assert out["properties"]["x"] == {"type": "string"}


# ── Tier inference ───────────────────────────────────────


def test_guess_tier_known_read() -> None:
    """Tools listed in autonomy.modes.TOOL_TIERS get the right tier."""
    assert _guess_tier("recall_memories") == ActionTier.READ
    assert _guess_tier("Read") == ActionTier.READ


def test_guess_tier_known_write() -> None:
    assert _guess_tier("store_memory") == ActionTier.WRITE
    assert _guess_tier("Edit") == ActionTier.WRITE


def test_guess_tier_known_destructive() -> None:
    assert _guess_tier("Bash") == ActionTier.DESTRUCTIVE
    assert _guess_tier("forget_memory") == ActionTier.DESTRUCTIVE


def test_guess_tier_unknown_defaults_to_write() -> None:
    """A tool not in the autonomy map defaults to WRITE — conservative
    default (semi_auto auto-allows; always_ask asks)."""
    assert _guess_tier("__brand_new_tool_no_one_has_classified__") == ActionTier.WRITE


# ── Timeout heuristics ──────────────────────────────────


def test_guess_timeout_slow_drafts() -> None:
    """draft_*, render_*, analyze_reference_site etc. are slow."""
    assert _guess_timeout("draft_deck", "creators") == 120
    assert _guess_timeout("render_deck_pdf", "creators") == 120
    assert _guess_timeout("analyze_reference_site", "creators") == 120
    assert _guess_timeout("critique_artifact", "creators") == 120


def test_guess_timeout_browser_moderate() -> None:
    """Network-bound — moderate timeout."""
    assert _guess_timeout("browser_fetch", "browser") == 30
    assert _guess_timeout("email_search", "email") == 30


def test_guess_timeout_default_fast() -> None:
    """DB-bound or pure-CPU — fast default."""
    assert _guess_timeout("recall_memories", "memory") == 15


# ── Bulk import ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_import_sdk_tools_registers_with_metadata() -> None:
    """A list of fake SdkMcpTools should land in the registry with the
    right name/description/schema/tier/namespace."""
    registry = ToolRegistry()

    async def handler1(args: dict) -> dict:
        return {"content": [{"type": "text", "text": "ok"}]}

    async def handler2(args: dict) -> dict:
        return {"content": [{"type": "text", "text": "ok2"}]}

    tools = [
        _FakeSdkTool(
            "recall_memories",
            "Search Astra's long-term memory by semantic similarity.",
            {"query": str, "top_k": int},
            handler1,
        ),
        _FakeSdkTool(
            "draft_deck",
            "Draft a slide deck for a portfolio company.",
            {"business_slug": str, "audience_slug": str},
            handler2,
        ),
    ]

    # Patch REGISTRY in the adapter module to use our fresh registry
    import astra.runtime.sdk_adapter as adapter

    adapter.REGISTRY = registry
    try:
        count = import_sdk_tools(tools, namespace="memory")
        # ↑ namespace label is for the FIRST tool's namespace; the
        # adapter assigns the same namespace to ALL tools in the list.
        # That's intentional — each call site groups ONE MCP server
        # at a time, so the namespace is per-call. recall_memories +
        # draft_deck would be in separate calls in real usage.
    finally:
        # Restore the original global registry reference
        from astra.runtime.tool_registry import REGISTRY as real_registry
        adapter.REGISTRY = real_registry

    assert count == 2

    rm = registry.get("recall_memories")
    assert rm is not None
    assert rm.tier == ActionTier.READ  # known in autonomy map
    assert rm.namespace == "memory"
    assert rm.timeout_sec == 15  # default fast
    assert rm.input_schema["properties"]["query"] == {"type": "string"}
    assert rm.input_schema["properties"]["top_k"] == {"type": "integer"}

    dd = registry.get("draft_deck")
    assert dd is not None
    assert dd.timeout_sec == 120  # slow heuristic — draft_*


@pytest.mark.asyncio
async def test_import_sdk_tools_skip_existing_idempotent() -> None:
    """Re-importing the same tools should not duplicate or raise."""
    registry = ToolRegistry()

    async def handler(args: dict) -> dict:
        return {"content": [{"type": "text", "text": "ok"}]}

    tool = _FakeSdkTool("only_once", "d", {}, handler)

    import astra.runtime.sdk_adapter as adapter

    adapter.REGISTRY = registry
    try:
        first = import_sdk_tools([tool], namespace="test")
        second = import_sdk_tools([tool], namespace="test")
    finally:
        from astra.runtime.tool_registry import REGISTRY as real_registry
        adapter.REGISTRY = real_registry

    assert first == 1
    assert second == 0
    assert len(registry.all()) == 1


@pytest.mark.asyncio
async def test_import_sdk_tools_dispatch_works(monkeypatch) -> None:
    """End-to-end: import a fake SDK tool, dispatch it through the
    registry, get the result back. Verifies that the SDK function
    body works through the registry's normalization."""
    registry = ToolRegistry()

    async def double(args: dict) -> dict:
        n = int(args.get("n", 0))
        return {
            "content": [{"type": "text", "text": f"doubled: {n * 2}"}]
        }

    tool = _FakeSdkTool(
        "double",
        "Double a number",
        {"n": int},
        double,
    )

    import astra.runtime.sdk_adapter as adapter

    adapter.REGISTRY = registry
    try:
        import_sdk_tools([tool], namespace="test")
    finally:
        from astra.runtime.tool_registry import REGISTRY as real_registry
        adapter.REGISTRY = real_registry

    result = await registry.dispatch("double", {"n": 21})
    assert result.is_error is False
    assert result.text == "doubled: 42"


@pytest.mark.asyncio
async def test_import_sdk_tools_missing_handler_skipped() -> None:
    """A malformed SdkMcpTool with no handler must not crash the
    bulk import — just log and continue."""
    registry = ToolRegistry()

    bad = _FakeSdkTool("no_handler", "d", {}, None)

    async def good_handler(args: dict) -> dict:
        return {"content": [{"type": "text", "text": "ok"}]}

    good = _FakeSdkTool("good", "d", {}, good_handler)

    import astra.runtime.sdk_adapter as adapter

    adapter.REGISTRY = registry
    try:
        count = import_sdk_tools([bad, good], namespace="test")
    finally:
        from astra.runtime.tool_registry import REGISTRY as real_registry
        adapter.REGISTRY = real_registry

    # Only `good` should be registered; `bad` skipped
    assert count == 1
    assert registry.get("no_handler") is None
    assert registry.get("good") is not None
