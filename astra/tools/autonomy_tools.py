"""
MCP tools for Astra's autonomy system.

Allows Astra (and the user through Astra) to:
- Check the current autonomy mode
- Switch modes (with time-based or task-based scoping)
- View the audit log
- Get audit statistics
"""

from astra.runtime.sdk_compat import tool, create_sdk_mcp_server

from astra.autonomy.audit import audit_logger
from astra.autonomy.manager import autonomy_manager
from astra.autonomy.modes import AutonomyMode


@tool(
    "get_mode",
    "Get the current autonomy mode and status. Shows current mode, "
    "whether a time-based revert is pending, and any task scope.",
    {},
)
async def get_mode_tool(args: dict) -> dict:
    status = autonomy_manager.get_status()
    lines = [
        f"Current mode: {status['current_mode']}",
        f"Previous mode: {status['previous_mode'] or 'none'}",
    ]
    if status["time_remaining_minutes"] is not None:
        lines.append(f"Time remaining: {status['time_remaining_minutes']} minutes")
    if status["task_scope"]:
        lines.append(f"Task scope: {status['task_scope']}")

    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


@tool(
    "set_mode",
    "Change the autonomy mode. Modes: 'always_ask' (ask for every action), "
    "'semi_auto' (auto-approve reads/writes, ask for destructive), "
    "'full_auto' (everything auto-approved). Optionally set a duration "
    "(minutes) after which it reverts, or a task_id for task-scoped mode.",
    {
        "mode": str,
        "duration_minutes": int,
        "task_id": str,
        "reason": str,
    },
)
async def set_mode_tool(args: dict) -> dict:
    mode_str = args["mode"]
    duration = args.get("duration_minutes", None)
    task_id = args.get("task_id", None)
    reason = args.get("reason", "User requested")

    try:
        mode = AutonomyMode(mode_str)
    except ValueError:
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Invalid mode '{mode_str}'. Must be: always_ask, semi_auto, full_auto",
                }
            ],
            "is_error": True,
        }

    result = autonomy_manager.set_mode(
        mode=mode,
        duration_minutes=duration,
        task_id=task_id,
        reason=reason,
    )

    msg = f"Mode changed: {result['from']} → {result['to']}"
    if duration:
        msg += f" (reverting in {duration} minutes)"
    if task_id:
        msg += f" (scoped to task: {task_id})"

    return {"content": [{"type": "text", "text": msg}]}


@tool(
    "get_audit_log",
    "View recent audit log entries. Shows what actions were taken, "
    "when, and what permission decision was made. Filter by tool name "
    "or decision type.",
    {
        "limit": int,
        "tool_name": str,
        "decision": str,
    },
)
async def get_audit_log_tool(args: dict) -> dict:
    limit = args.get("limit", 20)
    tool_name = args.get("tool_name", None)
    decision_str = args.get("decision", None)

    decision = None
    if decision_str:
        from astra.autonomy.modes import PermissionDecision

        try:
            decision = PermissionDecision(decision_str)
        except ValueError:
            pass

    entries = audit_logger.get_entries(
        limit=limit, tool_name=tool_name, decision=decision
    )

    if not entries:
        return {"content": [{"type": "text", "text": "No audit entries found."}]}

    lines = [f"Audit log ({len(entries)} entries):\n"]
    for e in entries:
        lines.append(
            f"[{e['timestamp']}] {e['tool_name']} ({e['action_tier']}) "
            f"→ {e['decision']} | mode={e['autonomy_mode']}"
        )
        if e.get("tool_input_summary"):
            lines.append(f"  input: {e['tool_input_summary'][:100]}")

    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


@tool(
    "audit_stats",
    "Get statistics about Astra's action history. Shows totals by "
    "decision type, action tier, and most-used tools.",
    {},
)
async def audit_stats_tool(args: dict) -> dict:
    stats = audit_logger.get_stats()

    lines = [
        f"Total actions logged: {stats['total']}",
        f"By decision: {stats['by_decision']}",
        f"By tier: {stats['by_tier']}",
        f"Top tools: {stats['by_tool']}",
    ]

    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


def create_autonomy_mcp_server():
    """Create the MCP server for autonomy tools."""
    return create_sdk_mcp_server(
        name="astra-autonomy",
        version="0.1.0",
        tools=[
            get_mode_tool,
            set_mode_tool,
            get_audit_log_tool,
            audit_stats_tool,
        ],
    )
