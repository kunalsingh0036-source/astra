"""
Tool implementations registered with the runtime registry.

This package's __init__ side-effect-registers EVERY tool the legacy
SDK exposed, so the lean runtime has full parity with the SDK runtime
without rewriting any tool bodies. The mechanism:

  1. Each `astra/tools/X_tools.py` file defines @tool-decorated
     functions and exposes them via a constructor (e.g.
     `create_X_mcp_server()` → returns object with `.tools` list)
     or a module constant (e.g. `KIT_EDITOR_TOOLS = [...]`).
  2. We import each module's tool list and pass it to
     `import_sdk_tools(...)` which registers each with REGISTRY.
  3. Tool bodies, schemas, and decorators in astra/tools/*.py are
     UNCHANGED — both runtimes can dispatch the same code.

Why constructors instead of explicit function-name imports:
  The previous bridge listed every tool's function name inline,
  e.g. `from astra.tools.notes_tools import notes_search_tool,
  notes_recent_tool, notes_count_tool`. When the source file was
  refactored (notes_recent → notes_list, notes_count → notes_sync,
  research_search → research, etc.), the import names went stale
  but the try/except wrapper silently swallowed the ImportError.
  Result: 5 entire namespaces (notes, tasks, research, system,
  artifacts) had ALL their tools silently missing from production
  for an unknown number of weeks.

  Pulling from `create_X_mcp_server().tools` makes the source file
  the single source of truth — when a tool is added/renamed there,
  it flows through here automatically. The bridge can no longer
  drift.

Phase 4 of the lean-runtime migration: this is where the agent loop
gets its full tool surface.
"""

import logging

from astra.runtime.sdk_adapter import import_sdk_tools

# Phase 1 ports: tools we wrote directly against the registry. These
# don't need the adapter.
from astra.runtime.tools import memory  # noqa: F401

# Phase 7: local-machine bridge tools — operate on Kunal's Mac via
# the bridge daemon. Side-effect-registers local_read, local_write,
# local_edit, local_bash, local_glob, local_grep, local_bridge_status,
# screenshot_url.
from astra.runtime.tools import local  # noqa: F401

logger = logging.getLogger(__name__)


def _import_sdk_namespace(namespace: str, sdk_tools: list) -> int:
    """Register an SDK MCP server's tool list with our registry."""
    return import_sdk_tools(sdk_tools, namespace=namespace)


def _bridge_constructor(namespace: str, mcp_server_factory) -> None:
    """Pull the tool list from a create_*_mcp_server() factory and
    register all of its tools under the given namespace.
    Wraps in try/except so a single broken module can't kill the
    whole bridge — but the LOG level is critical so failures aren't
    invisible the way the old explicit-imports version was.
    """
    try:
        server = mcp_server_factory()
        tools = list(getattr(server, "tools", []))
        if not tools:
            logger.warning(
                "[runtime/tools] %s: factory returned empty tool list",
                namespace,
            )
            return
        n = _import_sdk_namespace(namespace, tools)
        logger.info(
            "[runtime/tools] %s: registered %d tool(s)", namespace, n
        )
    except Exception:
        logger.exception(
            "[runtime/tools] %s: bridge failed; tools UNAVAILABLE", namespace
        )


def _bridge_constant(namespace: str, importer) -> None:
    """For modules that don't expose a factory but DO expose a
    module-level list constant (CODE_EDITOR_TOOLS, KIT_EDITOR_TOOLS,
    SELF_IMPROVE_TOOLS). `importer` is a no-arg callable that does
    the import + returns the list."""
    try:
        tools = list(importer())
        if not tools:
            logger.warning(
                "[runtime/tools] %s: empty tool list constant", namespace
            )
            return
        n = _import_sdk_namespace(namespace, tools)
        logger.info(
            "[runtime/tools] %s: registered %d tool(s)", namespace, n
        )
    except Exception:
        logger.exception(
            "[runtime/tools] %s: bridge failed; tools UNAVAILABLE", namespace
        )


# ── Bulk import of legacy SDK tools ─────────────────────────
#
# Each call below pulls a tool list from its source-of-truth
# constructor or constant. Tool names, function names, and schemas
# all live in the source file; this bridge no longer maintains a
# parallel list that can drift.

# Memory has a constructor too but is also Phase-1-ported via the
# local `memory` module above; we register the SDK ones to ensure
# every legacy memory tool is available.
def _imp_memory():
    from astra.tools.memory_tools import create_memory_mcp_server
    return create_memory_mcp_server()
_bridge_constructor("memory", _imp_memory)


def _imp_shares():
    from astra.tools.shares_tools import create_shares_mcp_server
    return create_shares_mcp_server()
_bridge_constructor("shares", _imp_shares)


def _imp_calendar():
    from astra.tools.calendar_tools import create_calendar_mcp_server
    return create_calendar_mcp_server()
_bridge_constructor("calendar", _imp_calendar)


def _imp_email():
    from astra.tools.email_tools import create_email_mcp_server
    return create_email_mcp_server()
_bridge_constructor("email", _imp_email)


def _imp_browser():
    from astra.tools.browser_tools import create_browser_mcp_server
    return create_browser_mcp_server()
_bridge_constructor("browser", _imp_browser)


def _imp_artifacts():
    from astra.tools.artifact_tools import create_artifact_mcp_server
    return create_artifact_mcp_server()
_bridge_constructor("artifacts", _imp_artifacts)


def _imp_autonomy():
    from astra.tools.autonomy_tools import create_autonomy_mcp_server
    return create_autonomy_mcp_server()
_bridge_constructor("autonomy", _imp_autonomy)


def _imp_fleet():
    from astra.tools.agent_fleet_tools import create_fleet_mcp_server
    return create_fleet_mcp_server()
_bridge_constructor("fleet", _imp_fleet)


def _imp_notes():
    from astra.tools.notes_tools import create_notes_mcp_server
    return create_notes_mcp_server()
_bridge_constructor("notes", _imp_notes)


def _imp_tasks():
    from astra.tools.task_tools import create_task_mcp_server
    return create_task_mcp_server()
_bridge_constructor("tasks", _imp_tasks)


def _imp_research():
    from astra.tools.research_tools import create_research_mcp_server
    return create_research_mcp_server()
_bridge_constructor("research", _imp_research)


def _imp_creators():
    from astra.tools.creator_tools import create_creators_mcp_server
    return create_creators_mcp_server()
_bridge_constructor("creators", _imp_creators)


def _imp_system():
    from astra.tools.system_tools import create_system_mcp_server
    return create_system_mcp_server()
_bridge_constructor("system", _imp_system)


def _imp_services():
    from astra.tools.service_tools import create_service_mcp_server
    return create_service_mcp_server()
_bridge_constructor("services", _imp_services)


def _imp_a2a():
    from astra.tools.a2a_tools import create_a2a_mcp_server
    return create_a2a_mcp_server()
_bridge_constructor("a2a", _imp_a2a)


def _imp_business():
    from astra.tools.business_state_tools import create_business_state_mcp_server
    return create_business_state_mcp_server()
_bridge_constructor("business", _imp_business)


# Modules that don't expose a constructor but publish a module-level
# tool list constant. Previously NOT BRIDGED AT ALL — these 19 tools
# were silently absent from the lean runtime.
def _imp_code_editor():
    from astra.tools.code_editor_tools import CODE_EDITOR_TOOLS
    return CODE_EDITOR_TOOLS
_bridge_constant("code_editor", _imp_code_editor)


def _imp_kit_editor():
    from astra.tools.kit_editor_tools import KIT_EDITOR_TOOLS
    return KIT_EDITOR_TOOLS
_bridge_constant("kit_editor", _imp_kit_editor)


def _imp_self_improve():
    from astra.tools.self_improve_tools import SELF_IMPROVE_TOOLS
    return SELF_IMPROVE_TOOLS
_bridge_constant("self_improve", _imp_self_improve)


# Total tools registered — handy for a startup log
from astra.runtime.tool_registry import REGISTRY as _registry

logger.info(
    "[runtime/tools] %d tools registered across %d namespaces",
    len(_registry.all()),
    len({t.namespace for t in _registry.all()}),
)
