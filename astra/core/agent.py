"""
Main Astra agent definition.

Configures the Claude Agent SDK with:
- System prompt (personality and rules)
- Custom MCP tools (memory, autonomy, fleet management)
- Hooks (autonomy enforcement, audit logging)
- Permission mode
- Model selection

This is where everything comes together.
"""

from claude_agent_sdk import ClaudeAgentOptions, HookMatcher

from astra.autonomy.hooks import autonomy_pre_tool_hook, audit_post_tool_hook
from astra.core.system_prompt import get_system_prompt
from astra.tools.memory_tools import create_memory_mcp_server
from astra.tools.autonomy_tools import create_autonomy_mcp_server
from astra.tools.agent_fleet_tools import create_fleet_mcp_server
from astra.tools.system_tools import create_system_mcp_server
from astra.tools.a2a_tools import create_a2a_mcp_server
from astra.tools.service_tools import create_service_mcp_server
from astra.tools.artifact_tools import create_artifact_mcp_server
from astra.tools.browser_tools import create_browser_mcp_server
from astra.tools.notes_tools import create_notes_mcp_server
from astra.tools.calendar_tools import create_calendar_mcp_server
from astra.tools.research_tools import create_research_mcp_server
from astra.tools.creator_tools import create_creators_mcp_server
from astra.tools.email_tools import create_email_mcp_server
from astra.tools.shares_tools import create_shares_mcp_server
from astra.tools.task_tools import create_task_mcp_server
from astra.agents.definitions.research_intel import (
    get_agent_definition as get_research_intel_definition,
    register as register_research_intel,
)
from astra.agents.external.registry import register_all_external_agents


def create_astra_options(resume_session_id: str | None = None) -> ClaudeAgentOptions:
    """Create the Agent SDK options for Astra.

    Args:
        resume_session_id: If provided, resume that SDK session so the
            agent keeps its prior conversation state. When None, a fresh
            session is started.

    Returns a fully configured ClaudeAgentOptions that can be passed
    to the SDK's query() function to start Astra.
    """
    # Register sub-agents in the fleet registry
    register_research_intel()

    # Register external agents (bookkeeper, linkedin, helmtech, apex)
    register_all_external_agents()

    # Create custom MCP servers for Astra's unique capabilities
    memory_server = create_memory_mcp_server()
    autonomy_server = create_autonomy_mcp_server()
    fleet_server = create_fleet_mcp_server()
    system_server = create_system_mcp_server()
    a2a_server = create_a2a_mcp_server()
    service_server = create_service_mcp_server()
    artifact_server = create_artifact_mcp_server()
    task_server = create_task_mcp_server()
    browser_server = create_browser_mcp_server()
    notes_server = create_notes_mcp_server()
    calendar_server = create_calendar_mcp_server()
    research_server = create_research_mcp_server()
    email_server = create_email_mcp_server()
    shares_server = create_shares_mcp_server()
    creators_server = create_creators_mcp_server()

    options = ClaudeAgentOptions(
        # System prompt defines Astra's identity and behavior
        system_prompt=get_system_prompt(),

        # Built-in tools Astra can use.
        #
        # ToolSearch was removed deliberately: with all of Astra's tools
        # explicitly registered + allowed below, there are no deferred
        # tools for the agent to look up. Including ToolSearch in the
        # menu just nudged Claude to "search for the right tool" as a
        # warm-up move on every turn, adding ~350ms + a thinking step
        # for no value. If we later add deferred MCP servers, reinstate.
        tools=[
            "Read",
            "Edit",
            "Write",
            "Glob",
            "Grep",
            "Bash",
            "WebSearch",
            "WebFetch",
            "Agent",
            "TodoWrite",
        ],

        # Custom MCP servers (memory, autonomy, fleet, system)
        mcp_servers={
            "astra-memory": memory_server,
            "astra-autonomy": autonomy_server,
            "astra-fleet": fleet_server,
            "astra-system": system_server,
            "astra-a2a": a2a_server,
            "astra-services": service_server,
            "astra-artifacts": artifact_server,
            "astra-tasks": task_server,
            "astra-browser": browser_server,
            "astra-notes": notes_server,
            "astra-calendar": calendar_server,
            "astra-research": research_server,
            "astra-email": email_server,
            "astra-shares": shares_server,
            "astra-creators": creators_server,
        },

        # Allow all custom MCP tools
        allowed_tools=[
            # Memory tools
            "mcp__astra-memory__store_memory",
            "mcp__astra-memory__recall_memories",
            "mcp__astra-memory__forget_memory",
            "mcp__astra-memory__list_memories",
            "mcp__astra-memory__memory_stats",
            # Autonomy tools
            "mcp__astra-autonomy__get_mode",
            "mcp__astra-autonomy__set_mode",
            "mcp__astra-autonomy__get_audit_log",
            "mcp__astra-autonomy__audit_stats",
            # Fleet tools
            "mcp__astra-fleet__list_agents",
            "mcp__astra-fleet__agent_status",
            "mcp__astra-fleet__recommend_agent",
            "mcp__astra-fleet__fleet_summary",
            # System tools
            "mcp__astra-system__system_info",
            "mcp__astra-system__health_check",
            # Scheduler trigger tools
            "mcp__astra-system__trigger_briefing",
            "mcp__astra-system__trigger_fleet_health",
            "mcp__astra-system__trigger_consolidation",
            # Tunnel tools
            "mcp__astra-system__start_tunnel",
            "mcp__astra-system__stop_tunnel",
            "mcp__astra-system__tunnel_status",
            # A2A protocol tools
            "mcp__astra-a2a__discover_agent",
            "mcp__astra-a2a__send_a2a_task",
            "mcp__astra-a2a__get_a2a_task",
            "mcp__astra-a2a__cancel_a2a_task",
            "mcp__astra-a2a__list_discovered_agents",
            "mcp__astra-a2a__a2a_health_check",
            # Service management tools
            "mcp__astra-services__start_service",
            "mcp__astra-services__stop_service",
            "mcp__astra-services__start_fleet",
            "mcp__astra-services__stop_fleet",
            "mcp__astra-services__fleet_status",
            "mcp__astra-services__fleet_health",
            "mcp__astra-services__service_logs",
            # Artifact emitters — always allowed, purely presentational
            "mcp__astra-artifacts__emit_table",
            "mcp__astra-artifacts__emit_draft",
            "mcp__astra-artifacts__emit_metric",
            # Task tools
            "mcp__astra-tasks__add_task",
            "mcp__astra-tasks__list_tasks",
            "mcp__astra-tasks__complete_task",
            # Browser tools
            "mcp__astra-browser__browser_fetch",
            "mcp__astra-browser__browser_search",
            # Apple Notes tools
            "mcp__astra-notes__notes_search",
            "mcp__astra-notes__notes_list",
            "mcp__astra-notes__notes_get",
            "mcp__astra-notes__notes_sync",
            # Google Calendar tools (read-only)
            "mcp__astra-calendar__calendar_status",
            "mcp__astra-calendar__calendar_today",
            "mcp__astra-calendar__calendar_tomorrow",
            "mcp__astra-calendar__calendar_week",
            "mcp__astra-calendar__calendar_search",
            # Research Intel — the compass + self aware agent
            "mcp__astra-research__research",
            "mcp__astra-research__research_list",
            "mcp__astra-research__research_get",
            # Email reading (sending stays in email-agent with approval)
            "mcp__astra-email__email_digest",
            "mcp__astra-email__email_unanswered",
            "mcp__astra-email__email_search",
            "mcp__astra-email__email_top_senders",
            "mcp__astra-email__email_classify_sweep",
            # Shares — what Kunal pushed in via iOS Share Sheet
            "mcp__astra-shares__list_recent_shares",
            "mcp__astra-shares__search_shares",
            "mcp__astra-shares__get_share",
            # Creators — brand-aware deck/doc/one-pager generation per company
            "mcp__astra-creators__list_business_kits",
            "mcp__astra-creators__read_business_kit",
            "mcp__astra-creators__draft_deck",
            "mcp__astra-creators__render_deck_pdf",
            "mcp__astra-creators__list_creator_artifacts",
            # Built-in read-only tools (safe to always allow)
            "Read",
            "Glob",
            "Grep",
        ],

        # Hooks for autonomy enforcement and audit logging
        hooks={
            "PreToolUse": [
                HookMatcher(
                    hooks=[autonomy_pre_tool_hook],
                    # Match all tools for autonomy check
                )
            ],
            "PostToolUse": [
                HookMatcher(
                    hooks=[audit_post_tool_hook],
                )
            ],
        },

        # Permission mode — our custom hooks handle fine-grained autonomy
        permission_mode="acceptEdits",

        # Budget and limits
        max_turns=50,

        # Sub-agents — registered fleet
        agents={
            "research-intel": get_research_intel_definition(),
        },

        # Multi-turn: resume the prior session so the agent remembers the
        # running conversation. None → fresh session.
        resume=resume_session_id,
    )

    return options
