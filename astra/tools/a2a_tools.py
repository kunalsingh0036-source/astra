"""
MCP tools for A2A protocol operations.

These tools let Astra (the LLM) interact with A2A agents through
the conversational interface. Instead of Astra calling Python code
directly, the LLM invokes these tools and the A2A client handles
the HTTP communication.

Tools:
- discover_agent: Discover an agent at a URL
- send_a2a_task: Send a task to a discovered agent
- get_a2a_task: Check status of a running task
- cancel_a2a_task: Cancel a task
- list_discovered_agents: Show all discovered agents
- a2a_health_check: Check if an agent is alive
"""

from astra.runtime.sdk_compat import tool, create_sdk_mcp_server

from astra.a2a.client import A2AClient
from astra.a2a.discovery import agent_discovery
from astra.a2a.exceptions import (
    A2AError,
    AgentNotFoundError,
    TaskFailedError,
    TaskTimeoutError,
)

# Shared client instance
_client = A2AClient(discovery=agent_discovery)


@tool(
    "discover_agent",
    "Discover an A2A agent at a given URL. Fetches the agent's Agent Card "
    "to learn its name, capabilities, and skills. Must be called before "
    "sending tasks to a new agent. Agents already discovered are cached.",
    {"url": str},
)
async def discover_agent_tool(args: dict) -> dict:
    url = args["url"]
    try:
        agent = await agent_discovery.discover(url)
        skills = [
            f"  - {s.id}: {s.name} — {s.description}"
            for s in agent.card.skills
        ]
        skills_text = "\n".join(skills) if skills else "  (no skills advertised)"

        text = (
            f"Discovered agent '{agent.card.name}' at {url}\n"
            f"Description: {agent.card.description}\n"
            f"Version: {agent.card.version}\n"
            f"Model tier: {agent.card.model_tier}\n"
            f"Skills:\n{skills_text}\n"
            f"Capabilities: streaming={agent.card.capabilities.streaming}, "
            f"push={agent.card.capabilities.push_notifications}"
        )
        return {"content": [{"type": "text", "text": text}]}
    except A2AError as e:
        return {"content": [{"type": "text", "text": str(e)}], "is_error": True}


@tool(
    "send_a2a_task",
    "Send a task to a discovered A2A agent. The agent will process it and "
    "return a result. Use discover_agent first if the agent hasn't been "
    "discovered yet. Set wait=true (default) to get the result immediately, "
    "or wait=false for async tasks you'll check later.",
    {
        "agent_name": str,
        "message": str,
        "skill_id": str,
        "priority": int,
        "timeout_seconds": int,
        "wait": bool,
    },
)
async def send_a2a_task_tool(args: dict) -> dict:
    agent_name = args["agent_name"]
    message = args["message"]
    skill_id = args.get("skill_id", None)
    priority = args.get("priority", 5)
    timeout = args.get("timeout_seconds", 300)
    wait = args.get("wait", True)

    try:
        task = await _client.send_task(
            agent_name=agent_name,
            message=message,
            skill_id=skill_id,
            priority=priority,
            timeout_seconds=timeout,
            wait=wait,
        )

        lines = [
            f"Task {task.id[:8]} → {agent_name}",
            f"State: {task.state.value}",
        ]

        if task.result:
            result_text = (
                task.result.content
                if isinstance(task.result.content, str)
                else str(task.result.content)
            )
            lines.append(f"Result:\n{result_text}")
        elif task.error:
            lines.append(f"Error: {task.error}")
        elif not wait:
            lines.append("Task submitted (not waiting). Use get_a2a_task to check.")

        return {"content": [{"type": "text", "text": "\n".join(lines)}]}

    except TaskTimeoutError as e:
        return {
            "content": [{"type": "text", "text": f"Timeout: {e}"}],
            "is_error": True,
        }
    except TaskFailedError as e:
        return {
            "content": [{"type": "text", "text": f"Failed: {e}"}],
            "is_error": True,
        }
    except A2AError as e:
        return {"content": [{"type": "text", "text": str(e)}], "is_error": True}


@tool(
    "get_a2a_task",
    "Check the current status of an A2A task. Use this to poll async tasks "
    "that were sent with wait=false.",
    {"agent_name": str, "task_id": str},
)
async def get_a2a_task_tool(args: dict) -> dict:
    agent_name = args["agent_name"]
    task_id = args["task_id"]

    try:
        task = await _client.get_task(agent_name, task_id)
        lines = [
            f"Task {task.id[:8]} on '{agent_name}'",
            f"State: {task.state.value}",
            f"Messages: {len(task.messages)}",
            f"Created: {task.created_at.isoformat()}",
        ]
        if task.result:
            result_text = (
                task.result.content
                if isinstance(task.result.content, str)
                else str(task.result.content)
            )
            lines.append(f"Result:\n{result_text}")
        if task.error:
            lines.append(f"Error: {task.error}")

        return {"content": [{"type": "text", "text": "\n".join(lines)}]}
    except A2AError as e:
        return {"content": [{"type": "text", "text": str(e)}], "is_error": True}


@tool(
    "cancel_a2a_task",
    "Cancel a running A2A task on a remote agent.",
    {"agent_name": str, "task_id": str},
)
async def cancel_a2a_task_tool(args: dict) -> dict:
    agent_name = args["agent_name"]
    task_id = args["task_id"]

    try:
        task = await _client.cancel_task(agent_name, task_id)
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Task {task.id[:8]} cancelled on '{agent_name}'",
                }
            ]
        }
    except A2AError as e:
        return {"content": [{"type": "text", "text": str(e)}], "is_error": True}


@tool(
    "list_discovered_agents",
    "List all A2A agents that Astra has discovered. Shows their name, URL, "
    "skills, and health status.",
    {},
)
async def list_discovered_agents_tool(args: dict) -> dict:
    agents = agent_discovery.list_all()

    if not agents:
        return {
            "content": [
                {
                    "type": "text",
                    "text": "No A2A agents discovered yet. "
                    "Use discover_agent with a URL to find agents.",
                }
            ]
        }

    lines = [f"Discovered {len(agents)} A2A agents:\n"]
    for a in agents:
        info = a.to_dict()
        lines.append(f"  {info['name']} — {info['url']}")
        lines.append(f"    Skills: {', '.join(info['skills']) or 'none'}")
        lines.append(f"    Healthy: {info['healthy']}")
        lines.append("")

    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


@tool(
    "a2a_health_check",
    "Check if an A2A agent is alive and responding. "
    "Pass 'all' as the name to check every discovered agent.",
    {"agent_name": str},
)
async def a2a_health_check_tool(args: dict) -> dict:
    name = args["agent_name"]

    if name == "all":
        results = await agent_discovery.health_check_all()
        if not results:
            return {
                "content": [
                    {"type": "text", "text": "No agents to check."}
                ]
            }
        lines = ["Health check results:"]
        for agent_name, healthy in results.items():
            status = "healthy" if healthy else "UNREACHABLE"
            lines.append(f"  {agent_name}: {status}")
        return {"content": [{"type": "text", "text": "\n".join(lines)}]}
    else:
        healthy = await agent_discovery.health_check(name)
        status = "healthy" if healthy else "UNREACHABLE"
        return {
            "content": [
                {"type": "text", "text": f"Agent '{name}': {status}"}
            ]
        }


def create_a2a_mcp_server():
    """Create the MCP server for A2A protocol tools."""
    return create_sdk_mcp_server(
        name="astra-a2a",
        version="0.1.0",
        tools=[
            discover_agent_tool,
            send_a2a_task_tool,
            get_a2a_task_tool,
            cancel_a2a_task_tool,
            list_discovered_agents_tool,
            a2a_health_check_tool,
        ],
    )
