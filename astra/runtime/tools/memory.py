"""
Memory tools registered with the runtime registry.

Phase 1 of the lean-runtime migration: port one simple, frequently-
used tool (recall_recent_turns) so we can validate the dispatch path
end-to-end before tackling the more complex tools.

The SDK wrapper in astra.tools.memory_tools currently has its own
implementation. To keep both paths working in lock-step, the SDK
wrapper will be updated in Phase 4 to delegate to these registry-
backed impls. For now both exist independently — the SDK path is
unchanged, the registry path is new.
"""

from __future__ import annotations

from sqlalchemy import text

from astra.db.engine import async_session
from astra.runtime.tool_registry import ActionTier, register_tool


@register_tool(
    name="recall_recent_turns",
    description=(
        "Pull the most recent chat turns from the turns table — "
        "DETERMINISTIC, not embedding-based. Use for queries about "
        "RECENCY: 'what did we just talk about', 'pull up our last "
        "conversation', 'what was I asking earlier'. Returns each "
        "turn's prompt, response (truncated), status, and timestamp. "
        "Embedding-based recall_memories is the wrong tool for these "
        "queries — it surfaces semantically-similar items, not most "
        "recent. Often misses brand-new conversations because the "
        "post-turn extraction hook hasn't fired yet."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "Number of turns to return (1-20). Defaults to 5.",
            },
            "session_id": {
                "type": "string",
                "description": (
                    "Optional — restrict to turns from a specific session. "
                    "Omit to return turns across all sessions."
                ),
            },
        },
    },
    tier=ActionTier.READ,
    timeout_sec=10,  # SQL with LIMIT and an index — should be <100ms
    namespace="memory",
)
async def recall_recent_turns_impl(args: dict) -> str:
    """Read the turns table directly — no LLM, no embedding."""
    limit = max(1, min(20, int(args.get("limit") or 5)))
    session_id = (args.get("session_id") or "").strip() or None

    sql = """
        SELECT id, session_id, prompt, response, status,
               started_at, ended_at, duration_ms, tool_count
        FROM turns
        {where}
        ORDER BY started_at DESC
        LIMIT :lim
    """
    if session_id:
        sql = sql.replace("{where}", "WHERE session_id = :sid")
        params: dict = {"lim": limit, "sid": session_id}
    else:
        sql = sql.replace("{where}", "")
        params = {"lim": limit}

    async with async_session() as s:
        r = await s.execute(text(sql), params)
        rows = r.all()

    if not rows:
        return (
            "No turns recorded yet — the turns table was added recently and "
            "only captures conversations from this point forward."
        )

    lines: list[str] = [f"Last {len(rows)} turn(s), newest first:\n"]
    for row in rows:
        prompt_short = (row.prompt or "")[:200].replace("\n", " ")
        response_short = (row.response or "")[:300].replace("\n", " ")
        ts = (
            row.started_at.strftime("%Y-%m-%d %H:%M UTC")
            if row.started_at
            else "—"
        )
        dur = (
            f" · {row.duration_ms / 1000:.1f}s"
            if row.duration_ms is not None
            else ""
        )
        tools = (
            f" · {row.tool_count} tool(s)" if (row.tool_count or 0) > 0 else ""
        )
        lines.append(
            f"--- turn #{row.id} · {ts}{dur}{tools} · status={row.status} ---\n"
            f"YOU: {prompt_short}\n"
            f"ASTRA: {response_short or '(no response — interrupted or failed)'}\n"
        )

    return "\n".join(lines)
