"""
Tool implementations registered with the runtime registry.

This package's __init__ side-effect-registers EVERY tool the legacy
SDK exposed, so the lean runtime has full parity with the SDK runtime
without rewriting any tool bodies. The mechanism:

  1. Each `astra/tools/X_tools.py` file contains @tool-decorated
     functions. After import, those functions are SdkMcpTool instances.
  2. We collect them per-module and pass the list to
     `import_sdk_tools(...)` which registers each with REGISTRY.
  3. Tool bodies, schemas, and decorators in astra/tools/*.py are
     UNCHANGED — both runtimes can dispatch the same code.

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
# local_edit, local_bash, local_glob, local_grep, local_bridge_status.
from astra.runtime.tools import local  # noqa: F401

logger = logging.getLogger(__name__)


def _import_sdk_namespace(namespace: str, sdk_tools: list) -> int:
    """Register an SDK MCP server's tool list with our registry."""
    return import_sdk_tools(sdk_tools, namespace=namespace)


# ── Bulk import of legacy SDK tools ─────────────────────────
#
# Each branch imports the @tool-decorated objects from the
# corresponding astra/tools/*.py module and hands them to the
# adapter. Wrapped in try/except so a breaking change in any
# single tool file doesn't kill the lean runtime — the rest
# of the tools still work.

try:
    from astra.tools.memory_tools import (
        store_memory_tool,
        recall_memories_tool,
        forget_memory_tool,
        list_memories_tool,
        memory_stats_tool,
        recall_recent_turns_tool,
    )

    _import_sdk_namespace(
        "memory",
        [
            store_memory_tool,
            recall_memories_tool,
            forget_memory_tool,
            list_memories_tool,
            memory_stats_tool,
            recall_recent_turns_tool,
        ],
    )
except Exception:
    logger.exception("[runtime/tools] failed to import memory tools")

try:
    from astra.tools.shares_tools import (
        list_recent_shares_tool,
        get_share_tool,
        search_shares_tool,
    )

    _import_sdk_namespace(
        "shares",
        [list_recent_shares_tool, get_share_tool, search_shares_tool],
    )
except Exception:
    logger.exception("[runtime/tools] failed to import shares tools")

try:
    from astra.tools.calendar_tools import (
        calendar_today_tool,
        calendar_tomorrow_tool,
        calendar_week_tool,
        calendar_search_tool,
        calendar_status_tool,
    )

    _import_sdk_namespace(
        "calendar",
        [
            calendar_today_tool,
            calendar_tomorrow_tool,
            calendar_week_tool,
            calendar_search_tool,
            calendar_status_tool,
        ],
    )
except Exception:
    logger.exception("[runtime/tools] failed to import calendar tools")

try:
    from astra.tools.email_tools import (
        email_unanswered_tool,
        email_search_tool,
        email_top_senders_tool,
        email_classify_sweep_tool,
        email_digest_tool,
    )

    _import_sdk_namespace(
        "email",
        [
            email_unanswered_tool,
            email_search_tool,
            email_top_senders_tool,
            email_classify_sweep_tool,
            email_digest_tool,
        ],
    )
except Exception:
    logger.exception("[runtime/tools] failed to import email tools")

try:
    from astra.tools.browser_tools import (
        browser_fetch_tool,
        browser_search_tool,
    )

    _import_sdk_namespace(
        "browser",
        [browser_fetch_tool, browser_search_tool],
    )
except Exception:
    logger.exception("[runtime/tools] failed to import browser tools")

try:
    from astra.tools.artifact_tools import create_artifact_mcp_server

    # Pull the tool list from the MCP server constructor instead of
    # naming each tool individually. Adding a new emit_* tool there
    # (e.g. emit_palette, prepare_preview, future ones) automatically
    # gets bridged here — eliminates the "tool registered with the
    # SDK decorator but missing from the lean dispatch registry"
    # bug class. Previous failure: emit_palette + prepare_preview
    # shipped in artifact_tools.py but were missed in this list →
    # agent saw them in its tool list (good) but dispatch crashed
    # with `unknown tool: 'emit_palette'` (bad).
    _import_sdk_namespace(
        "artifacts",
        list(create_artifact_mcp_server().tools),
    )
except Exception:
    logger.exception("[runtime/tools] failed to import artifact tools")

try:
    from astra.tools.autonomy_tools import (
        get_mode_tool,
        set_mode_tool,
        get_audit_log_tool,
        audit_stats_tool,
    )

    _import_sdk_namespace(
        "autonomy",
        [
            get_mode_tool,
            set_mode_tool,
            get_audit_log_tool,
            audit_stats_tool,
        ],
    )
except Exception:
    logger.exception("[runtime/tools] failed to import autonomy tools")

try:
    from astra.tools.agent_fleet_tools import (
        list_agents_tool,
        agent_status_tool,
        recommend_agent_tool,
        fleet_summary_tool,
    )

    _import_sdk_namespace(
        "fleet",
        [
            list_agents_tool,
            agent_status_tool,
            recommend_agent_tool,
            fleet_summary_tool,
        ],
    )
except Exception:
    logger.exception("[runtime/tools] failed to import fleet tools")

try:
    from astra.tools.notes_tools import (
        notes_search_tool,
        notes_recent_tool,
        notes_get_tool,
        notes_count_tool,
    )

    _import_sdk_namespace(
        "notes",
        [
            notes_search_tool,
            notes_recent_tool,
            notes_get_tool,
            notes_count_tool,
        ],
    )
except Exception:
    logger.exception("[runtime/tools] failed to import notes tools")

try:
    from astra.tools.task_tools import (
        list_tasks_tool,
        get_task_tool,
        update_task_tool,
    )

    _import_sdk_namespace(
        "tasks",
        [list_tasks_tool, get_task_tool, update_task_tool],
    )
except Exception:
    logger.exception("[runtime/tools] failed to import task tools")

try:
    from astra.tools.research_tools import (
        research_list_tool,
        research_get_tool,
        research_search_tool,
    )

    _import_sdk_namespace(
        "research",
        [research_list_tool, research_get_tool, research_search_tool],
    )
except Exception:
    logger.exception("[runtime/tools] failed to import research tools")

try:
    from astra.tools.creator_tools import (
        list_business_kits_tool,
        read_business_kit_tool,
        list_creator_artifacts_tool,
        analyze_reference_site_tool,
        # Drafts
        draft_deck_tool,
        draft_doc_tool,
        draft_one_pager_tool,
        draft_brand_kit_tool,
        draft_carousel_tool,
        draft_thread_tool,
        draft_caption_set_tool,
        draft_hashtag_set_tool,
        draft_video_brief_tool,
        draft_voiceover_script_tool,
        draft_subtitle_set_tool,
        draft_site_brief_tool,
        draft_page_content_tool,
        draft_component_spec_tool,
        # Critique
        critique_artifact_tool,
        # Renders
        render_deck_pdf_tool,
        render_one_pager_pdf_tool,
        render_doc_pdf_tool,
        render_site_preview_tool,
        # Image generation
        generate_image_tool,
    )

    _import_sdk_namespace(
        "creators",
        [
            list_business_kits_tool,
            read_business_kit_tool,
            list_creator_artifacts_tool,
            analyze_reference_site_tool,
            draft_deck_tool,
            draft_doc_tool,
            draft_one_pager_tool,
            draft_brand_kit_tool,
            draft_carousel_tool,
            draft_thread_tool,
            draft_caption_set_tool,
            draft_hashtag_set_tool,
            draft_video_brief_tool,
            draft_voiceover_script_tool,
            draft_subtitle_set_tool,
            draft_site_brief_tool,
            draft_page_content_tool,
            draft_component_spec_tool,
            critique_artifact_tool,
            render_deck_pdf_tool,
            render_one_pager_pdf_tool,
            render_doc_pdf_tool,
            render_site_preview_tool,
            generate_image_tool,
        ],
    )
except Exception:
    logger.exception("[runtime/tools] failed to import creator tools")

try:
    from astra.tools.system_tools import (
        get_time_tool,
        system_info_tool,
        health_check_tool,
        cost_report_tool,
        list_recent_chats_tool,
        get_recent_thoughts_tool,
        environment_summary_tool,
        astra_status_tool,
    )

    _import_sdk_namespace(
        "system",
        [
            get_time_tool,
            system_info_tool,
            health_check_tool,
            cost_report_tool,
            list_recent_chats_tool,
            get_recent_thoughts_tool,
            environment_summary_tool,
            astra_status_tool,
        ],
    )
except Exception:
    logger.exception("[runtime/tools] failed to import system tools")

try:
    from astra.tools.service_tools import (
        start_service_tool,
        stop_service_tool,
        start_fleet_tool,
        stop_fleet_tool,
        fleet_status_tool,
        fleet_health_tool,
        service_logs_tool,
    )

    _import_sdk_namespace(
        "services",
        [
            start_service_tool,
            stop_service_tool,
            start_fleet_tool,
            stop_fleet_tool,
            fleet_status_tool,
            fleet_health_tool,
            service_logs_tool,
        ],
    )
except Exception:
    logger.exception("[runtime/tools] failed to import service tools")

try:
    from astra.tools.a2a_tools import (
        discover_agent_tool,
        send_a2a_task_tool,
        get_a2a_task_tool,
        cancel_a2a_task_tool,
        list_discovered_agents_tool,
        a2a_health_check_tool,
    )

    _import_sdk_namespace(
        "a2a",
        [
            discover_agent_tool,
            send_a2a_task_tool,
            get_a2a_task_tool,
            cancel_a2a_task_tool,
            list_discovered_agents_tool,
            a2a_health_check_tool,
        ],
    )
except Exception:
    logger.exception("[runtime/tools] failed to import a2a tools")

# Total tools registered — handy for a startup log
from astra.runtime.tool_registry import REGISTRY as _registry

logger.info(
    "[runtime/tools] %d tools registered across %d namespaces",
    len(_registry.all()),
    len({t.namespace for t in _registry.all()}),
)
