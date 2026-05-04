"""
Astra lean runtime — direct Anthropic Messages API + custom tool dispatch.

This module is the migration target for moving off the Claude Agent SDK.
It's being built incrementally; until Phase 5 the SDK path remains the
production runtime. See docs in /docs/runtime-migration.md (TBD) for
phase-by-phase progress.

Phase 1 (current): tool registry foundation. The registry is purely
additive — both the SDK and the future lean runtime can dispatch from
the same registered functions.
"""

from astra.runtime.tool_registry import (
    ActionTier,
    REGISTRY,
    ToolDef,
    ToolRegistry,
    ToolResult,
    register_tool,
)

__all__ = [
    "ActionTier",
    "REGISTRY",
    "ToolDef",
    "ToolRegistry",
    "ToolResult",
    "register_tool",
]
