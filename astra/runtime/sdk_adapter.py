"""
SDK → Registry adapter.

The legacy SDK exposes tools via `@tool` from claude_agent_sdk, which
turns the decorated function into an `SdkMcpTool` instance with
fields: name, description, input_schema, handler.

This adapter introspects those instances and registers them with
the runtime ToolRegistry. The same decorated function therefore
becomes available to BOTH the legacy SDK CLI (via the existing MCP
servers) AND the lean runtime (via REGISTRY.dispatch). Zero rewrites
needed for the tool bodies — only the dispatch layer changes.

Schema conversion: SDK accepts schemas as either Python-types dicts
(`{"name": str, "count": int}`) or JSON Schema dicts. The lean
runtime sends tools to Anthropic's Messages API which requires JSON
Schema, so we normalize on import.

Tier inference: SDK tool tiers (READ/WRITE/DESTRUCTIVE) live in
astra.autonomy.modes.TOOL_TIERS for SDK-known names. We reuse that
mapping when porting; tools not in the map default to WRITE.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

from astra.runtime.tool_registry import ActionTier, REGISTRY, ToolDef

logger = logging.getLogger(__name__)


# Python type → JSON Schema type-string. Falls through to "string"
# for unknown types so the schema is at least valid (the model will
# pass values as strings; the tool can parse).
_PY_TYPE_MAP = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


def _is_json_schema(schema: Any) -> bool:
    """Detect whether a schema is already in JSON Schema form (has
    a top-level 'type' key with the value 'object'), vs the SDK's
    Python-type-dict shorthand ({"field": str, ...})."""
    if not isinstance(schema, dict):
        return False
    if schema.get("type") in ("object", "string", "number", "integer", "boolean", "array"):
        return True
    return False


def _to_json_schema(schema: Any) -> dict[str, Any]:
    """Convert SDK schema → JSON Schema dict for Anthropic API."""
    if schema is None:
        return {"type": "object", "properties": {}}
    if _is_json_schema(schema):
        return schema  # already correct shape
    if isinstance(schema, dict):
        # SDK shorthand: {"field": str, "count": int, ...}
        properties: dict[str, dict[str, Any]] = {}
        for field_name, py_type in schema.items():
            if isinstance(py_type, dict):
                # Mixed: a dict where one field is itself a JSON-schema fragment
                properties[field_name] = py_type
            else:
                properties[field_name] = {
                    "type": _PY_TYPE_MAP.get(py_type, "string"),
                }
        return {"type": "object", "properties": properties}
    # Type alias (e.g. some pydantic class) — best-effort fallback.
    return {"type": "object", "properties": {}}


def _guess_tier(tool_name: str) -> ActionTier:
    """Look up the tool in the autonomy tier map; default to WRITE."""
    try:
        from astra.autonomy.modes import TOOL_TIERS, ActionTier as AutonomyTier
    except Exception:
        return ActionTier.WRITE
    auto_tier = TOOL_TIERS.get(tool_name)
    if auto_tier is None:
        return ActionTier.WRITE
    # The autonomy module's ActionTier enum has identical values to
    # ours but is a different class. Map by value.
    return ActionTier(auto_tier.value)


def _guess_timeout(tool_name: str, namespace: str) -> int:
    """Per-tool timeout heuristic. Most tools are fast lookups (10s);
    a few are known slow paths (LLM-adjacent generation, multi-step
    fetch+analyze). Tunable per-name later if needed."""
    # Slow tools — generation + crawl
    SLOW = (
        "draft_",
        "render_",
        "analyze_reference_site",
        "critique",
        "generate",
    )
    for prefix in SLOW:
        if tool_name.startswith(prefix):
            return 120
    # Slow by exact name — their bodies own longer inner budgets (bridge
    # reads / LLM distillation) that the 15s default would cancel.
    SLOW_EXACT = {
        "ingest_voice_export": 180,   # paged Mac-bridge reads + parse + POST
        "learn_my_voice": 150,        # LLM distillation
        "mine_my_voice": 300,         # kicks off background mine
    }
    if tool_name in SLOW_EXACT:
        return SLOW_EXACT[tool_name]
    # Browser fetches — moderate
    if "browser" in tool_name or "fetch" in tool_name or "search" in tool_name:
        return 30
    # Everything else — DB-bound or fast
    return 15


def import_sdk_tools(
    sdk_tools: Iterable[Any],
    *,
    namespace: str,
    skip_existing: bool = True,
) -> int:
    """Register a batch of SDK-decorated tools with the runtime registry.

    Args:
        sdk_tools: iterable of SdkMcpTool instances (the result of
            @tool-decorated functions in claude_agent_sdk).
        namespace: registry namespace label (e.g. "memory", "creators").
            Used for grouping in tool subsets when an agent flow only
            needs a portion of the tool surface.
        skip_existing: if True (default), don't re-register tools whose
            names already exist in the registry. Lets tool files be
            imported in any order without duplicate-registration errors.

    Returns: count of tools newly registered.
    """
    count = 0
    for sdk_tool in sdk_tools:
        # SDK's SdkMcpTool has these fields on every instance.
        name = getattr(sdk_tool, "name", None)
        if not name:
            continue

        if skip_existing and REGISTRY.get(name) is not None:
            continue

        description = getattr(sdk_tool, "description", "") or ""
        input_schema = _to_json_schema(getattr(sdk_tool, "input_schema", None))
        handler = getattr(sdk_tool, "handler", None)
        if handler is None:
            logger.warning(
                "[sdk-adapter] %s has no handler — skipping registration",
                name,
            )
            continue

        REGISTRY.register(
            ToolDef(
                name=name,
                description=description,
                input_schema=input_schema,
                fn=handler,
                tier=_guess_tier(name),
                namespace=namespace,
                timeout_sec=_guess_timeout(name, namespace),
            )
        )
        count += 1

    if count > 0:
        logger.info(
            "[sdk-adapter] imported %d tools into namespace=%s",
            count,
            namespace,
        )
    return count
