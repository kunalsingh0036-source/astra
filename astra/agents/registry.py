"""
Agent fleet registry.

Tracks all registered sub-agents, their capabilities, status, and metadata.
This is how Astra knows what agents are available and what they can do.

Agents are registered here and also exposed to the Agent SDK as subagent
definitions. The registry is the source of truth for the fleet.
"""

import threading
from datetime import datetime, timezone
from enum import Enum


class AgentStatus(str, Enum):
    ACTIVE = "active"          # Ready to receive tasks
    BUILDING = "building"      # Under development
    PROPOSED = "proposed"      # Recommended but not built yet
    DISABLED = "disabled"      # Built but turned off


class AgentDefinitionRecord:
    """Metadata about a registered agent."""

    __slots__ = (
        "name",
        "description",
        "capabilities",
        "status",
        "tools",
        "model_tier",
        "created_at",
        "last_used",
        "usage_count",
        "build_complexity",
    )

    def __init__(
        self,
        name: str,
        description: str,
        capabilities: list[str],
        status: AgentStatus = AgentStatus.PROPOSED,
        tools: list[str] | None = None,
        model_tier: str = "sonnet",
        build_complexity: str = "medium",
    ):
        self.name = name
        self.description = description
        self.capabilities = capabilities
        self.status = status
        self.tools = tools or []
        self.model_tier = model_tier
        self.created_at = datetime.now(timezone.utc)
        self.last_used = None
        self.usage_count = 0
        self.build_complexity = build_complexity

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "capabilities": self.capabilities,
            "status": self.status.value,
            "tools": self.tools,
            "model_tier": self.model_tier,
            "created_at": self.created_at.isoformat(),
            "last_used": self.last_used.isoformat() if self.last_used else None,
            "usage_count": self.usage_count,
            "build_complexity": self.build_complexity,
        }


class AgentRegistry:
    """Registry for all sub-agents in Astra's fleet."""

    def __init__(self):
        self._agents: dict[str, AgentDefinitionRecord] = {}
        self._lock = threading.Lock()

    def register(self, agent: AgentDefinitionRecord) -> None:
        """Register or update an agent in the fleet."""
        with self._lock:
            self._agents[agent.name] = agent

    def get(self, name: str) -> AgentDefinitionRecord | None:
        """Get an agent by name."""
        return self._agents.get(name)

    def list_all(self, status: AgentStatus | None = None) -> list[dict]:
        """List all agents, optionally filtered by status."""
        with self._lock:
            agents = list(self._agents.values())

        if status:
            agents = [a for a in agents if a.status == status]

        return [a.to_dict() for a in agents]

    def record_usage(self, name: str) -> None:
        """Record that an agent was used."""
        agent = self._agents.get(name)
        if agent:
            agent.usage_count += 1
            agent.last_used = datetime.now(timezone.utc)

    def get_fleet_summary(self) -> dict:
        """Get a summary of the fleet status."""
        with self._lock:
            agents = list(self._agents.values())

        return {
            "total": len(agents),
            "active": sum(1 for a in agents if a.status == AgentStatus.ACTIVE),
            "building": sum(1 for a in agents if a.status == AgentStatus.BUILDING),
            "proposed": sum(1 for a in agents if a.status == AgentStatus.PROPOSED),
            "disabled": sum(1 for a in agents if a.status == AgentStatus.DISABLED),
        }


# Global singleton
agent_registry = AgentRegistry()
