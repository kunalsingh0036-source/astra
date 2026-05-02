"""
MCP tools for system-level information, health checks, and scheduler control.

Provides Astra with self-awareness about its own system:
- System info (versions, config, uptime)
- Health check (database, redis, services)
- Scheduler triggers (briefing, health check, consolidation, cost report)
- Tunnel management (start/stop/status webhook tunnel)
"""

import platform
import sys

from claude_agent_sdk import tool, create_sdk_mcp_server

from astra import __version__
from astra.config import settings


@tool(
    "system_info",
    "Get Astra's system information — version, Python version, platform, "
    "configured models, autonomy mode, and database URL.",
    {},
)
async def system_info_tool(args: dict) -> dict:
    from astra.autonomy.manager import autonomy_manager

    info = {
        "astra_version": __version__,
        "python_version": sys.version,
        "platform": platform.platform(),
        "models": {
            "opus": settings.model_opus,
            "sonnet": settings.model_sonnet,
            "haiku": settings.model_haiku,
        },
        "autonomy_mode": autonomy_manager.mode.value,
        "embedding_model": settings.embedding_model,
        "embedding_dimension": settings.embedding_dimension,
        "database": settings.database_url.split("@")[-1],  # hide credentials
    }

    lines = [f"{k}: {v}" for k, v in info.items()]
    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


@tool(
    "health_check",
    "Check the health of Astra's services — database connectivity, "
    "Redis connectivity, and embedding model availability.",
    {},
)
async def health_check_tool(args: dict) -> dict:
    checks = {}

    # Database check
    try:
        from astra.db.engine import engine
        from sqlalchemy import text

        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["database"] = "healthy"
    except Exception as e:
        checks["database"] = f"unhealthy: {e}"

    # Redis check
    try:
        import redis as redis_lib

        r = redis_lib.from_url(settings.redis_url)
        r.ping()
        checks["redis"] = "healthy"
    except Exception as e:
        checks["redis"] = f"unhealthy: {e}"

    # Embedding model check
    try:
        from astra.memory.embeddings import embed_text

        vec = embed_text("test")
        checks["embeddings"] = f"healthy (dim={len(vec)})"
    except Exception as e:
        checks["embeddings"] = f"unhealthy: {e}"

    overall = "healthy" if all("healthy" in v for v in checks.values()) else "degraded"
    lines = [f"Overall: {overall}"] + [f"  {k}: {v}" for k, v in checks.items()]

    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


# ---------------------------------------------------------------------------
# Scheduler trigger tools
# ---------------------------------------------------------------------------


@tool(
    "trigger_briefing",
    "Trigger the morning briefing task immediately instead of waiting for "
    "the scheduled 7:30 AM run. Generates a fleet + memory status summary "
    "and stores it as a memory.",
    {},
)
async def trigger_briefing_tool(args: dict) -> dict:
    from astra.scheduler.jobs import run_morning_briefing
    try:
        result = await run_morning_briefing()
        return {"content": [{"type": "text", "text": f"Briefing generated. Result: {result}"}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"Failed to run briefing: {e}"}]}


@tool(
    "trigger_fleet_health",
    "Trigger a fleet health check immediately. Probes every service's "
    "health endpoint and reports healthy / unhealthy / stopped counts.",
    {},
)
async def trigger_fleet_health_tool(args: dict) -> dict:
    from astra.scheduler.jobs import run_fleet_health_check
    try:
        result = await run_fleet_health_check()
        return {"content": [{"type": "text", "text": f"Health check: {result}"}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"Failed to run health check: {e}"}]}


@tool(
    "trigger_consolidation",
    "Trigger memory consolidation immediately instead of waiting for the "
    "3:00 AM nightly run. Steps: prune working memory → decay importance → "
    "prune low importance → merge duplicates → summarize old episodic clusters. "
    "Can take a few minutes on a large memory store.",
    {},
)
async def trigger_consolidation_tool(args: dict) -> dict:
    from astra.scheduler.jobs import run_memory_consolidation
    try:
        result = await run_memory_consolidation()
        return {"content": [{"type": "text", "text": f"Consolidation complete: {result}"}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"Failed to run consolidation: {e}"}]}


# ---------------------------------------------------------------------------
# Tunnel tools
# ---------------------------------------------------------------------------


@tool(
    "start_tunnel",
    "Start a webhook tunnel (ngrok or cloudflared) to expose the WhatsApp "
    "Gateway at localhost:8600 to the internet. Returns the public URL "
    "that can be registered with Meta as the webhook callback URL.",
    {"port": int},
)
async def start_tunnel_tool(args: dict) -> dict:
    from astra.services.tunnel import tunnel_manager
    port = args.get("port", 8600)
    result = tunnel_manager.start(port=port)

    status = result["status"]
    if status == "started":
        url = result.get("public_url", "unknown")
        webhook = result.get("webhook_url", "")
        text = (
            f"Tunnel started ({result['provider']})\n"
            f"Public URL: {url}\n"
            f"Webhook URL: {webhook}\n"
            f"PID: {result.get('pid', '?')}\n\n"
            f"Register this webhook URL in Meta App Dashboard → WhatsApp → Configuration"
        )
    elif status == "already_running":
        text = f"Tunnel already running. Public URL: {result.get('public_url', 'unknown')}"
    elif status == "error":
        text = f"Failed to start tunnel: {result['message']}"
    else:
        text = str(result)

    return {"content": [{"type": "text", "text": text}]}


@tool(
    "stop_tunnel",
    "Stop the running webhook tunnel.",
    {},
)
async def stop_tunnel_tool(args: dict) -> dict:
    from astra.services.tunnel import tunnel_manager
    result = tunnel_manager.stop()

    if result["status"] == "stopped":
        text = f"Tunnel stopped (was PID {result['pid']})"
    elif result["status"] == "not_running":
        text = "No tunnel is running"
    else:
        text = str(result)

    return {"content": [{"type": "text", "text": text}]}


@tool(
    "tunnel_status",
    "Check the current status of the webhook tunnel. Returns whether it's "
    "running, the provider, public URL, and webhook URL.",
    {},
)
async def tunnel_status_tool(args: dict) -> dict:
    from astra.services.tunnel import tunnel_manager
    result = tunnel_manager.status()

    if result["status"] == "running":
        lines = [
            f"Tunnel: running ({result['provider']})",
            f"PID: {result.get('pid', '?')}",
            f"Public URL: {result.get('public_url', 'unknown')}",
            f"Webhook URL: {result.get('webhook_url', 'unknown')}",
        ]
    else:
        lines = [
            f"Tunnel: stopped",
            f"Provider: {result['provider']}",
            "Start with: start_tunnel",
        ]

    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


def create_system_mcp_server():
    """Create the MCP server for system tools."""
    return create_sdk_mcp_server(
        name="astra-system",
        version="0.1.0",
        tools=[
            system_info_tool,
            health_check_tool,
            trigger_briefing_tool,
            trigger_fleet_health_tool,
            trigger_consolidation_tool,
            start_tunnel_tool,
            stop_tunnel_tool,
            tunnel_status_tool,
        ],
    )
