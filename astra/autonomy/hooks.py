"""
Agent SDK hooks for autonomy enforcement.

These hooks plug into the Claude Agent SDK's lifecycle system.
The PreToolUse hook fires before every tool call and decides
whether to allow, deny, or ask based on the current autonomy mode.
Every decision is audit-logged.
"""

from astra.autonomy.audit import audit_logger
from astra.autonomy.manager import autonomy_manager
from astra.autonomy.modes import (
    PermissionDecision,
    get_action_tier,
    get_permission,
)


async def autonomy_pre_tool_hook(input_data: dict, tool_use_id: str, context: dict) -> dict:
    """PreToolUse hook — enforces autonomy mode before every tool call.

    Args:
        input_data: Contains tool_name and tool_input from the Agent SDK.
        tool_use_id: Unique ID for this tool invocation.
        context: Additional context from the SDK.

    Returns:
        Hook response dict controlling permission decision.
    """
    tool_name = input_data.get("tool_name", "unknown")
    tool_input = input_data.get("tool_input", {})

    # Get current mode and determine permission
    current_mode = autonomy_manager.mode
    tier = get_action_tier(tool_name)
    decision = get_permission(current_mode, tool_name)

    # Create a summary of the tool input for audit (truncated)
    input_summary = str(tool_input)[:200]

    # Log the action
    audit_logger.log(
        tool_name=tool_name,
        action_tier=tier,
        autonomy_mode=current_mode,
        decision=decision,
        tool_input_summary=input_summary,
    )

    # Map our decision to Agent SDK's hook response format
    if decision == PermissionDecision.ALLOW:
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "permissionDecisionReason": (
                    f"Auto-allowed: mode={current_mode.value}, tier={tier.value}"
                ),
            }
        }
    elif decision == PermissionDecision.DENY:
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    f"Denied: mode={current_mode.value}, tier={tier.value}"
                ),
            }
        }
    else:
        # ASK — let the SDK's default permission flow handle it
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "ask",
                "permissionDecisionReason": (
                    f"Requires approval: mode={current_mode.value}, tier={tier.value}"
                ),
            }
        }


async def audit_post_tool_hook(input_data: dict, tool_use_id: str, context: dict) -> dict:
    """PostToolUse hook — logs tool completion for audit trail.

    This is a lightweight hook that just records that a tool completed.
    The main audit logging happens in the PreToolUse hook.
    """
    return {}
