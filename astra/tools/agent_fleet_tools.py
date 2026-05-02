"""
MCP tools for Astra's agent fleet management.

Allows Astra to:
- List all agents in the fleet
- Check agent status
- Get recommendations for which agent to build next
- Get fleet summary statistics
"""

from claude_agent_sdk import tool, create_sdk_mcp_server

from astra.agents.recommender import get_recommendations
from astra.agents.registry import AgentStatus, agent_registry


@tool(
    "list_agents",
    "List all agents in Astra's fleet with their status and capabilities. "
    "Filter by status: 'active', 'building', 'proposed', 'disabled'.",
    {"status": str},
)
async def list_agents_tool(args: dict) -> dict:
    status_str = args.get("status", None)
    status = None
    if status_str:
        try:
            status = AgentStatus(status_str)
        except ValueError:
            pass

    agents = agent_registry.list_all(status=status)

    if not agents:
        return {
            "content": [
                {
                    "type": "text",
                    "text": "No agents registered in the fleet yet. "
                    "Use recommend_agent to see what should be built first.",
                }
            ]
        }

    lines = [f"Fleet: {len(agents)} agents\n"]
    for a in agents:
        lines.append(
            f"[{a['status']}] {a['name']} — {a['description'][:80]}"
        )
        lines.append(f"  capabilities: {', '.join(a['capabilities'][:3])}")
        lines.append(f"  model: {a['model_tier']} | uses: {a['usage_count']}")

    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


@tool(
    "agent_status",
    "Get detailed status of a specific agent by name.",
    {"name": str},
)
async def agent_status_tool(args: dict) -> dict:
    name = args["name"]
    agent = agent_registry.get(name)

    if not agent:
        return {
            "content": [
                {"type": "text", "text": f"Agent '{name}' not found in fleet."}
            ],
            "is_error": True,
        }

    info = agent.to_dict()
    lines = [
        f"Agent: {info['name']}",
        f"Status: {info['status']}",
        f"Description: {info['description']}",
        f"Capabilities: {', '.join(info['capabilities'])}",
        f"Tools: {', '.join(info['tools'])}",
        f"Model tier: {info['model_tier']}",
        f"Usage count: {info['usage_count']}",
        f"Last used: {info['last_used'] or 'never'}",
    ]

    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


@tool(
    "recommend_agent",
    "Analyze current fleet and recommend which agent to build next. "
    "Returns prioritized recommendations with rationale and complexity estimate.",
    {"max_results": int},
)
async def recommend_agent_tool(args: dict) -> dict:
    max_results = args.get("max_results", 3)
    recommendations = get_recommendations(max_results=max_results)

    if not recommendations:
        return {
            "content": [
                {
                    "type": "text",
                    "text": "All proposed agents have been built. The fleet is complete.",
                }
            ]
        }

    lines = ["Agent build recommendations (highest priority first):\n"]
    for i, rec in enumerate(recommendations, 1):
        lines.append(f"{i}. **{rec['name']}** (priority: {rec['priority_score']:.2f})")
        lines.append(f"   {rec['description'][:100]}")
        lines.append(f"   Complexity: {rec['build_complexity']} | Model: {rec['model_tier']}")
        lines.append(f"   Rationale: {rec['rationale']}")
        lines.append("")

    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


@tool(
    "fleet_summary",
    "Get a high-level summary of Astra's agent fleet — total agents, "
    "how many are active, building, proposed, or disabled.",
    {},
)
async def fleet_summary_tool(args: dict) -> dict:
    summary = agent_registry.get_fleet_summary()
    lines = [
        f"Fleet summary:",
        f"  Total: {summary['total']}",
        f"  Active: {summary['active']}",
        f"  Building: {summary['building']}",
        f"  Proposed: {summary['proposed']}",
        f"  Disabled: {summary['disabled']}",
    ]
    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


def create_fleet_mcp_server():
    """Create the MCP server for fleet management tools."""
    return create_sdk_mcp_server(
        name="astra-fleet",
        version="0.1.0",
        tools=[
            list_agents_tool,
            agent_status_tool,
            recommend_agent_tool,
            fleet_summary_tool,
        ],
    )
