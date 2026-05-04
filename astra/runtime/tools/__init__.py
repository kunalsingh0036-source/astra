"""
Tool implementations registered with the runtime registry.

Importing this package side-effect-registers every tool. Both the SDK
adapter (legacy MCP servers) and the lean runtime call into these.

Phase 1: only `memory` is ported as a proof-of-concept. Subsequent
phases port the rest.
"""

# Side-effect imports — touch each module to fire @register_tool.
from astra.runtime.tools import memory  # noqa: F401
