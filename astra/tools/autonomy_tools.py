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

    # Persist to app_settings as well so the web UI / other services
    # see the change. Without this, an agent-driven mode switch was
    # invisible to the /settings page and to any subsequent web read.
    result = await autonomy_manager.set_mode_persisted(
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

    # DB-backed (calendared 2026-06-06 item, shipped with Phase C):
    # audit_events is what EVERY service writes and what the web
    # /audit page reads. The in-memory audit_logger only sees this
    # process — answers to "what did you do today" were blind to
    # cross-service activity. Falls back to the in-memory list if
    # the DB read fails.
    entries: list[dict] = []
    try:
        from sqlalchemy import text as _sql

        from astra.db.engine import async_session

        where = ["1=1"]
        params: dict = {"l": int(limit)}
        if tool_name:
            where.append("tool_name = :tn")
            params["tn"] = tool_name
        if decision:
            where.append("decision = :d")
            params["d"] = decision.value
        async with async_session() as s:
            r = await s.execute(
                _sql(
                    f"""
                    SELECT ts, tool_name, action_tier, autonomy_mode,
                           decision, tool_input_summary
                    FROM audit_events
                    WHERE {' AND '.join(where)}
                    ORDER BY ts DESC LIMIT :l
                    """
                ),
                params,
            )
            entries = [
                {
                    "timestamp": row.ts.isoformat(),
                    "tool_name": row.tool_name,
                    "action_tier": row.action_tier,
                    "autonomy_mode": row.autonomy_mode,
                    "decision": row.decision,
                    "tool_input_summary": row.tool_input_summary,
                }
                for row in r.fetchall()
            ]
    except Exception:
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
    # DB-backed cross-service stats; in-memory fallback.
    try:
        from sqlalchemy import text as _sql

        from astra.db.engine import async_session

        async with async_session() as s:
            total = (
                await s.execute(_sql("SELECT count(*) FROM audit_events"))
            ).scalar() or 0
            by_decision = dict(
                (
                    await s.execute(
                        _sql(
                            "SELECT decision, count(*) FROM audit_events "
                            "GROUP BY decision"
                        )
                    )
                ).fetchall()
            )
            by_tier = dict(
                (
                    await s.execute(
                        _sql(
                            "SELECT action_tier, count(*) FROM audit_events "
                            "GROUP BY action_tier"
                        )
                    )
                ).fetchall()
            )
            by_tool = dict(
                (
                    await s.execute(
                        _sql(
                            "SELECT tool_name, count(*) FROM audit_events "
                            "GROUP BY tool_name ORDER BY count(*) DESC LIMIT 10"
                        )
                    )
                ).fetchall()
            )
        stats = {
            "total": total,
            "by_decision": by_decision,
            "by_tier": by_tier,
            "by_tool": by_tool,
        }
    except Exception:
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
            list_pending_approvals_tool,
            resolve_approval_tool,
            revoke_tool_grant_tool,
        ],
    )


@tool(
    "list_pending_approvals",
    "List actions waiting for Kunal's approval. Each entry shows id, "
    "tool, input summary, and why it was gated. Use when the user "
    "asks 'what's waiting on me' or before resolving approvals.",
    {},
)
async def list_pending_approvals_tool(args: dict) -> dict:
    from astra.autonomy.approvals import list_pending

    rows = await list_pending()
    if not rows:
        return {"content": [{"type": "text", "text": "No pending approvals."}]}
    lines = [f"{len(rows)} pending approval(s):"]
    for r in rows:
        inp = str(r["tool_input"])[:120]
        lines.append(
            f"  #{r['id']} · {r['tool_name']} · {inp} · {r['reason']}"
        )
    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


@tool(
    "resolve_approval",
    "Approve or deny a pending action by id — THIS is how Kunal says "
    "yes/no from chat or WhatsApp ('approve 12', 'deny 12', 'approve "
    "12 always'). decision: 'approved' or 'denied'. standing=true "
    "grants the tool permanently (no more asking for that tool). "
    "After approving, re-run the original action — the grant is "
    "consumed by the next identical tool call.",
    {
        "approval_id": int,
        "decision": str,
        "standing": bool,
    },
)
async def resolve_approval_tool(args: dict) -> dict:
    from astra.autonomy.approvals import resolve_approval

    decision = str(args.get("decision", "")).strip().lower()
    if decision in ("approve", "yes", "y"):
        decision = "approved"
    if decision in ("deny", "no", "n", "reject"):
        decision = "denied"
    result = await resolve_approval(
        int(args["approval_id"]),
        decision,
        standing=bool(args.get("standing", False)),
        source="chat",
    )
    if not result.get("ok"):
        return {
            "content": [{"type": "text", "text": f"Failed: {result.get('error')}"}],
            "is_error": True,
        }
    extra = " (standing grant — won't ask again)" if result.get("standing") else ""
    return {
        "content": [
            {
                "type": "text",
                "text": (
                    f"Approval #{args['approval_id']} → {result['decision']} "
                    f"for {result['tool_name']}{extra}. Re-run the action "
                    "now if approved."
                ),
            }
        ]
    }


@tool(
    "revoke_tool_grant",
    "Remove a standing grant for a tool so it asks for approval "
    "again — the demotion path on the trust ladder.",
    {"tool_name": str},
)
async def revoke_tool_grant_tool(args: dict) -> dict:
    from astra.autonomy.approvals import revoke_grant

    found = await revoke_grant(str(args.get("tool_name", "")))
    text = (
        f"Standing grant for {args.get('tool_name')} revoked — it will "
        "ask for approval again."
        if found
        else f"No standing grant found for {args.get('tool_name')}."
    )
    return {"content": [{"type": "text", "text": text}]}
