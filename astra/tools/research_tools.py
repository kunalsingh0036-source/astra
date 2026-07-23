"""
MCP tools for Research Intel — the compass-aware + self-aware
research agent.

Three tools:
  * research(query, depth)        — invoke on-demand; blocks until ready
  * research_list(limit)          — recent briefings with gist + status
  * research_get(id)              — full body_md + signals + action items
"""

from __future__ import annotations

import logging

from astra.runtime.sdk_compat import create_sdk_mcp_server, tool

logger = logging.getLogger(__name__)


@tool(
    "research",
    "Invoke Research Intel — Astra's compass-aware + self-aware research "
    "agent. Reads Kunal's full compass (3 ambitions, 4 businesses, "
    "training, schedule) + Astra's internal state (jobs, services, DB, "
    "pending, recent) and produces a structured briefing with findings, "
    "signals, build/subtract recommendations, urgencies, and action "
    "items. Use when Kunal asks to research a topic, audit Astra, or "
    "decide what to build next. Costs real Anthropic tokens + web "
    "searches, so reserve for questions that genuinely need context, "
    "not quick lookups. Returns the briefing id and a summary.",
    {"query": str, "depth": str, "business_tags": str},
)
async def research_tool(args: dict) -> dict:
    from astra.research.runner import run_topic_on_demand

    q = (args.get("query") or "").strip()
    if not q:
        return {"content": [{"type": "text", "text": "research: query required"}]}
    # quick = legacy single-shot (cheap lookup). standard/deep run the
    # research AGENT: plan → parallel sourced sub-searches → code
    # guards → adversarial verify → synthesize. Default standard.
    depth = (args.get("depth") or "standard").lower()
    if depth not in ("quick", "standard", "deep"):
        depth = "standard"
    tags = (args.get("business_tags") or "").strip()[:255]

    # FIRE-AND-NOTIFY: the agent pipeline takes 3-6+ minutes, which no
    # chat turn can contain (the 240s turn hard cap serves Vercel's
    # 300s limit — turns #696/#697 died waiting). So the tool starts
    # the run as a shielded background task and returns the briefing id
    # IMMEDIATELY; when the run lands, the owner gets a WhatsApp ping
    # with the gist (never a silent drop). Check anytime: research_get.
    import asyncio

    async def _run_and_notify() -> None:
        try:
            result = await run_topic_on_demand(
                topic=q, depth=depth, business_tags=tags
            )
        except Exception:
            logger.exception("[research] background run crashed for %r", q)
            return
        status = result.get("status")
        rid = result.get("id", "?")
        if status == "ready":
            msg = (f"Research #{rid} ready — {q}\n"
                   f"{result.get('gist','')}\n"
                   f"Open: /research/{rid}")
        else:
            msg = (f"Research #{rid} failed — {q}\n"
                   f"{str(result.get('error',''))[:200]}")
        await _notify_owner(msg)

    asyncio.get_running_loop().create_task(_run_and_notify())
    text_out = (
        f"research started ({depth}) — {q}\n"
        "The agent pipeline (plan → parallel sourced searches → verify → "
        "synthesize) takes ~3-6 minutes. Kunal gets a WhatsApp ping with "
        "the gist when it lands; use research_list/research_get to read "
        "it. Tell Kunal it's running — do NOT wait or poll in this turn."
    )
    return {"content": [{"type": "text", "text": text_out}]}


async def _notify_owner(text_msg: str) -> None:
    """WhatsApp the owner via the gateway; loud log on failure, never
    a silent swallow (channel rule: alert on outcomes)."""
    import os

    import httpx

    base = os.environ.get(
        "GATEWAY_URL", "http://whatsapp.railway.internal:8080"
    ).rstrip("/")
    secret = os.environ.get("AGENT_SHARED_SECRET", "").strip()
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.post(
                f"{base}/api/v1/notify/owner",
                json={"text": text_msg[:3000]},
                headers={"x-astra-secret": secret},
            )
        if r.status_code != 200:
            logger.error("[research] owner notify failed: %s %s",
                         r.status_code, r.text[:200])
    except Exception:
        logger.exception("[research] owner notify errored")


@tool(
    "research_list",
    "List recent research briefings newest-first with status + gist. "
    "Use to see what's been researched lately before deciding to "
    "spawn a fresh brief.",
    {"limit": int, "business_tag": str},
)
async def research_list_tool(args: dict) -> dict:
    from sqlalchemy import text
    from astra.db.engine import async_session

    limit = max(1, min(20, int(args.get("limit") or 10)))
    btag = (args.get("business_tag") or "").strip()

    async with async_session() as s:
        if btag:
            r = await s.execute(
                text(
                    """
                    SELECT id, topic, kind, status, model_used,
                           LEFT(body_md, 260), created_at
                    FROM research_briefings
                    WHERE business_tags LIKE :tag
                    ORDER BY created_at DESC LIMIT :lim
                    """
                ),
                {"tag": f"%{btag}%", "lim": limit},
            )
        else:
            r = await s.execute(
                text(
                    """
                    SELECT id, topic, kind, status, model_used,
                           LEFT(body_md, 260), created_at
                    FROM research_briefings
                    ORDER BY created_at DESC LIMIT :lim
                    """
                ),
                {"lim": limit},
            )
        rows = r.all()

    if not rows:
        return {"content": [{"type": "text", "text": "no briefings yet"}]}

    lines = [f"{len(rows)} briefings:"]
    for row in rows:
        rid, topic, kind, status, model, head, created = row
        ts = created.strftime("%a %d %b %H:%M") if created else "—"
        lines.append(
            f"\n#{rid} [{status}] {ts} · {kind} · {topic[:80]}"
        )
        if head:
            lines.append(f"  {head.strip()[:220]}")
    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


@tool(
    "research_get",
    "Fetch one briefing's full body — includes findings, signals, "
    "build/subtract recommendations, urgencies, action items, sources.",
    {"id": int},
)
async def research_get_tool(args: dict) -> dict:
    from sqlalchemy import text
    from astra.db.engine import async_session

    rid = int(args.get("id") or 0)
    if rid <= 0:
        return {"content": [{"type": "text", "text": "research_get: id required"}]}
    async with async_session() as s:
        r = await s.execute(
            text(
                """
                SELECT topic, status, body_md, error, model_used,
                       duration_ms, created_at, completed_at
                FROM research_briefings WHERE id = :id
                """
            ),
            {"id": rid},
        )
        row = r.first()
    if not row:
        return {"content": [{"type": "text", "text": f"briefing #{rid} not found"}]}

    topic, status, body, err, model, dur, created, completed = row
    header = (
        f"# research #{rid} — {topic}\n"
        f"status: {status} · model: {model or '-'} · duration: {dur or '-'}ms\n"
        f"created: {created} · completed: {completed}\n"
    )
    if err:
        return {"content": [{"type": "text", "text": f"{header}\nerror: {err}"}]}
    return {"content": [{"type": "text", "text": f"{header}\n{body}"}]}


def create_research_mcp_server():
    return create_sdk_mcp_server(
        name="astra-research",
        version="0.1.0",
        tools=[research_tool, research_list_tool, research_get_tool],
    )
