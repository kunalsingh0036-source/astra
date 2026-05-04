"""
MCP tools for service management.

These tools let Astra start, stop, and monitor agent backend services
directly from conversation. No manual terminal work needed.

Tools:
- start_service: Start an agent backend
- stop_service: Stop an agent backend
- start_fleet: Start all agents + bridge
- stop_fleet: Stop everything
- fleet_status: Quick status of all services
- fleet_health: Deep health check (HTTP) of all services
- service_logs: Get recent logs for a service
"""

from astra.runtime.sdk_compat import tool, create_sdk_mcp_server

from astra.services.manager import service_manager, SERVICES


@tool(
    "start_service",
    "Start an agent backend service. Available services: bookkeeper, apex, "
    "linkedin, helmtech, whatsapp, bridge, celery. The bridge server is the "
    "A2A proxy that connects Astra to all agents. Celery is the scheduler "
    "for proactive tasks (briefings, health checks, consolidation).",
    {"name": str},
)
async def start_service_tool(args: dict) -> dict:
    name = args["name"]
    result = service_manager.start(name)

    status = result["status"]
    if status == "started":
        text = (
            f"Started {result['service']} (PID {result['pid']}) "
            f"on port {result['port']}\n"
            f"Logs: {result['log']}"
        )
    elif status == "already_running":
        text = (
            f"{result['service']} is already running "
            f"(PID {result['pid']}, port {result['port']})"
        )
    elif status == "error":
        text = f"Failed to start: {result['message']}"
    else:
        text = str(result)

    return {"content": [{"type": "text", "text": text}]}


@tool(
    "stop_service",
    "Stop a running agent backend service.",
    {"name": str},
)
async def stop_service_tool(args: dict) -> dict:
    name = args["name"]
    result = service_manager.stop(name)

    status = result["status"]
    if status == "stopped":
        text = f"Stopped {result['service']} (was PID {result['pid']})"
    elif status == "not_running":
        text = f"{result['service']} is not running"
    elif status == "error":
        text = f"Error: {result['message']}"
    else:
        text = str(result)

    return {"content": [{"type": "text", "text": text}]}


@tool(
    "start_fleet",
    "Start ALL agent backend services, the A2A bridge, and the scheduler. "
    "Starts agents first (bookkeeper, apex, linkedin, helmtech, whatsapp), "
    "then the bridge server and Celery worker. "
    "Equivalent to starting each service individually.",
    {},
)
async def start_fleet_tool(args: dict) -> dict:
    results = service_manager.start_all()

    lines = ["Fleet startup results:\n"]
    for r in results:
        status = r["status"]
        name = r.get("service", "Unknown")
        if status == "started":
            lines.append(f"  {name}: started (PID {r['pid']}, port {r['port']})")
        elif status == "already_running":
            lines.append(f"  {name}: already running (PID {r['pid']})")
        elif status == "error":
            lines.append(f"  {name}: FAILED — {r['message']}")
        else:
            lines.append(f"  {name}: {status}")

    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


@tool(
    "stop_fleet",
    "Stop ALL agent backend services and the A2A bridge. Stops bridge "
    "first, then all agent backends.",
    {},
)
async def stop_fleet_tool(args: dict) -> dict:
    results = service_manager.stop_all()

    lines = ["Fleet shutdown results:\n"]
    for r in results:
        status = r["status"]
        name = r.get("service", "Unknown")
        if status == "stopped":
            lines.append(f"  {name}: stopped (was PID {r['pid']})")
        elif status == "not_running":
            lines.append(f"  {name}: was not running")
        else:
            lines.append(f"  {name}: {status}")

    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


@tool(
    "fleet_status",
    "Quick status check of all agent services. Shows which are running, "
    "their PIDs, and ports. No HTTP calls — just PID checks.",
    {},
)
async def fleet_status_tool(args: dict) -> dict:
    statuses = service_manager.status_all()

    lines = ["Service Status:\n"]
    for s in statuses:
        icon = "🟢" if s["status"] == "running" else "⏹️"
        pid_info = f"PID {s['pid']}" if s["pid"] else "—"
        lines.append(
            f"  {icon} {s['service']:<25} port {s['port']}  {pid_info}"
        )

    running = sum(1 for s in statuses if s["status"] == "running")
    total = len(statuses)
    lines.append(f"\n{running}/{total} services running")

    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


@tool(
    "fleet_health",
    "Deep health check of all agent services. Makes HTTP requests to each "
    "service's health endpoint. Slower than fleet_status but confirms "
    "services are actually responding.",
    {},
)
async def fleet_health_tool(args: dict) -> dict:
    results = await service_manager.health_check_all()

    lines = ["Fleet Health Check:\n"]
    for r in results:
        status = r["status"]
        if status == "healthy":
            icon = "🟢"
        elif status == "stopped":
            icon = "⏹️"
        else:
            icon = "🔴"

        pid_info = f"PID {r.get('pid', '—')}" if r.get("pid") else "—"
        lines.append(
            f"  {icon} {r['service']:<25} {status:<10} port {r['port']}  {pid_info}"
        )

    healthy = sum(1 for r in results if r["status"] == "healthy")
    total = len(results)
    lines.append(f"\n{healthy}/{total} services healthy")

    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


@tool(
    "service_logs",
    "Get recent log output for a service. Shows both stdout and stderr. "
    "Useful for debugging startup failures or checking agent behavior.",
    {"name": str, "lines": int},
)
async def service_logs_tool(args: dict) -> dict:
    name = args["name"]
    num_lines = args.get("lines", 50)

    logs = service_manager.get_logs(name, lines=num_lines)
    return {"content": [{"type": "text", "text": logs}]}


def create_service_mcp_server():
    """Create the MCP server for service management tools."""
    return create_sdk_mcp_server(
        name="astra-services",
        version="0.1.0",
        tools=[
            start_service_tool,
            stop_service_tool,
            start_fleet_tool,
            stop_fleet_tool,
            fleet_status_tool,
            fleet_health_tool,
            service_logs_tool,
        ],
    )
