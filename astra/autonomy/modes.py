"""
Autonomy mode definitions.

Three modes that control how much Astra can do without asking:

1. ALWAYS_ASK — Every action requires user approval
2. SEMI_AUTO — Read-only actions auto-approved, destructive actions need approval
3. FULL_AUTO — Everything executes immediately, audit log generated

Each tool action is classified into a tier:
- READ: No side effects (file reads, searches, API GETs)
- WRITE: Modifiable but recoverable (file edits, memory updates)
- DESTRUCTIVE: Hard to reverse (file deletes, sending emails, API mutations)
"""

import enum


class AutonomyMode(str, enum.Enum):
    ALWAYS_ASK = "always_ask"
    SEMI_AUTO = "semi_auto"
    FULL_AUTO = "full_auto"


class ActionTier(str, enum.Enum):
    READ = "read"
    WRITE = "write"
    DESTRUCTIVE = "destructive"


class PermissionDecision(str, enum.Enum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


# Permission matrix: mode × action tier → decision
PERMISSION_MATRIX: dict[AutonomyMode, dict[ActionTier, PermissionDecision]] = {
    AutonomyMode.ALWAYS_ASK: {
        ActionTier.READ: PermissionDecision.ASK,
        ActionTier.WRITE: PermissionDecision.ASK,
        ActionTier.DESTRUCTIVE: PermissionDecision.ASK,
    },
    AutonomyMode.SEMI_AUTO: {
        ActionTier.READ: PermissionDecision.ALLOW,
        ActionTier.WRITE: PermissionDecision.ALLOW,
        ActionTier.DESTRUCTIVE: PermissionDecision.ASK,
    },
    AutonomyMode.FULL_AUTO: {
        ActionTier.READ: PermissionDecision.ALLOW,
        ActionTier.WRITE: PermissionDecision.ALLOW,
        ActionTier.DESTRUCTIVE: PermissionDecision.ALLOW,
    },
}


# Tool name → action tier mapping
# Tools not in this map default to WRITE tier
TOOL_TIERS: dict[str, ActionTier] = {
    # Read-only tools
    "Read": ActionTier.READ,
    "Glob": ActionTier.READ,
    "Grep": ActionTier.READ,
    "WebSearch": ActionTier.READ,
    "WebFetch": ActionTier.READ,
    "recall_memories": ActionTier.READ,
    "recall_recent_turns": ActionTier.READ,
    "list_memories": ActionTier.READ,
    "memory_stats": ActionTier.READ,
    "get_mode": ActionTier.READ,
    "get_audit_log": ActionTier.READ,
    "list_agents": ActionTier.READ,
    "agent_status": ActionTier.READ,
    "recommend_agent": ActionTier.READ,
    "system_info": ActionTier.READ,
    "health_check": ActionTier.READ,
    "cost_report": ActionTier.READ,
    # A2A protocol tools — reads
    "list_discovered_agents": ActionTier.READ,
    "get_a2a_task": ActionTier.READ,
    "a2a_health_check": ActionTier.READ,
    # Service management tools — reads
    "fleet_status": ActionTier.READ,
    "fleet_health": ActionTier.READ,
    "service_logs": ActionTier.READ,

    # Write tools
    "Edit": ActionTier.WRITE,
    "Write": ActionTier.WRITE,
    "store_memory": ActionTier.WRITE,
    "set_mode": ActionTier.WRITE,
    # A2A protocol tools — writes
    "discover_agent": ActionTier.WRITE,
    "send_a2a_task": ActionTier.WRITE,
    # Service management tools — writes
    "start_service": ActionTier.WRITE,
    "start_fleet": ActionTier.WRITE,

    # Read-only ops
    "agent_logs": ActionTier.READ,
    "fleet_status": ActionTier.READ,

    # Destructive tools
    "Bash": ActionTier.DESTRUCTIVE,
    "restart_agent": ActionTier.DESTRUCTIVE,
    "forget_memory": ActionTier.DESTRUCTIVE,
    # A2A protocol tools — destructive
    "cancel_a2a_task": ActionTier.DESTRUCTIVE,
    # Service management tools — destructive
    "stop_service": ActionTier.DESTRUCTIVE,
    "stop_fleet": ActionTier.DESTRUCTIVE,
}


def get_action_tier(tool_name: str) -> ActionTier:
    """Get the action tier for a tool. Defaults to WRITE if unknown."""
    return TOOL_TIERS.get(tool_name, ActionTier.WRITE)


def get_permission_for_tier(
    mode: AutonomyMode, tier: ActionTier
) -> PermissionDecision:
    """Permission decision for a KNOWN tier.

    This is the path the lean runtime uses: the tool registry already
    declares every tool's tier at registration (ToolDef.tier), so the
    gate must trust that — not the name-keyed TOOL_TIERS map below.
    The map only knows 36 legacy names out of 117 registered tools;
    everything else silently fell to WRITE, which auto-allowed
    local_bash (arbitrary shell on Kunal's Mac, registered
    DESTRUCTIVE) in semi_auto because the map only listed the old
    SDK name "Bash". Same split-brain class as the autonomy-mode bug
    fixed in 7374fd7: two sources of truth, the stale one consulted.
    """
    return PERMISSION_MATRIX[mode][tier]


def get_permission(mode: AutonomyMode, tool_name: str) -> PermissionDecision:
    """Name-based permission lookup — LEGACY path.

    Only for callers that genuinely have no ToolDef (SDK-era hooks,
    tests). Anything with access to the tool registry must use
    get_permission_for_tier with the registered tier instead; the
    name map here defaults unknown names to WRITE, which is exactly
    the silent-permission-bypass this split caused.
    """
    tier = get_action_tier(tool_name)
    return get_permission_for_tier(mode, tier)
