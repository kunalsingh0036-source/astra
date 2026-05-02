"""
Tests for A2A discovery and client.

Tests the agent discovery cache, local registration,
and the A2A client task management.
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from astra.a2a.discovery import AgentDiscovery, DiscoveredAgent
from astra.a2a.client import A2AClient
from astra.a2a.models import (
    AgentCard,
    AgentSkill,
    Message,
    MessagePart,
    MessageRole,
    Task,
    TaskState,
)
from astra.a2a.exceptions import (
    AgentNotFoundError,
    SkillNotFoundError,
    TaskFailedError,
)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


class TestAgentDiscovery:
    def test_register_local(self):
        """Register an agent without HTTP discovery."""
        discovery = AgentDiscovery()
        card = AgentCard(
            name="local-agent",
            description="A local test agent",
            url="http://localhost:8100",
            skills=[
                AgentSkill(
                    id="test-skill",
                    name="Test Skill",
                    description="Does testing",
                )
            ],
        )

        agent = discovery.register_local(card)

        assert agent.card.name == "local-agent"
        assert agent.healthy is True
        assert agent.discovered_at is not None

    def test_get_registered_agent(self):
        """Retrieve a registered agent by name."""
        discovery = AgentDiscovery()
        card = AgentCard(
            name="my-agent",
            description="Test",
            url="http://localhost:8100",
        )
        discovery.register_local(card)

        agent = discovery.get("my-agent")
        assert agent is not None
        assert agent.card.name == "my-agent"

    def test_get_missing_agent(self):
        """Returns None for unknown agent."""
        discovery = AgentDiscovery()
        assert discovery.get("nonexistent") is None

    def test_list_all(self):
        """List all discovered agents."""
        discovery = AgentDiscovery()
        for i in range(3):
            card = AgentCard(
                name=f"agent-{i}",
                description=f"Agent {i}",
                url=f"http://localhost:{8100 + i}",
            )
            discovery.register_local(card)

        agents = discovery.list_all()
        assert len(agents) == 3

    def test_list_healthy(self):
        """Filter to only healthy agents."""
        discovery = AgentDiscovery()
        for i in range(3):
            card = AgentCard(
                name=f"agent-{i}",
                description=f"Agent {i}",
                url=f"http://localhost:{8100 + i}",
            )
            agent = discovery.register_local(card)
            if i == 1:
                agent.healthy = False

        healthy = discovery.list_healthy()
        assert len(healthy) == 2

    def test_remove_agent(self):
        """Remove an agent from discovery cache."""
        discovery = AgentDiscovery()
        card = AgentCard(
            name="removable",
            description="Will be removed",
            url="http://localhost:8100",
        )
        discovery.register_local(card)

        assert discovery.remove("removable") is True
        assert discovery.get("removable") is None
        assert discovery.remove("removable") is False  # Already gone

    def test_to_dict(self):
        """DiscoveredAgent serializes to dict."""
        discovery = AgentDiscovery()
        card = AgentCard(
            name="dict-test",
            description="Test serialization",
            url="http://localhost:8100",
            skills=[
                AgentSkill(
                    id="s1", name="Skill 1", description="Does s1"
                )
            ],
        )
        agent = discovery.register_local(card)
        d = agent.to_dict()

        assert d["name"] == "dict-test"
        assert d["url"] == "http://localhost:8100"
        assert d["skills"] == ["s1"]
        assert d["healthy"] is True
        assert "discovered_at" in d


# ---------------------------------------------------------------------------
# Client — local task management
# ---------------------------------------------------------------------------


class TestA2AClientLocal:
    """Test client methods that don't require HTTP (local cache, history)."""

    def test_get_local_tasks_empty(self):
        """No tasks initially."""
        client = A2AClient()
        assert client.get_local_tasks() == []

    def test_get_task_history_empty(self):
        """Empty history."""
        client = A2AClient()
        assert client.get_task_history() == []

    def test_get_local_tasks_filtered(self):
        """Filter local tasks by agent and state."""
        client = A2AClient()

        # Manually add tasks to the local cache
        t1 = Task(agent_name="agent-a", state=TaskState.COMPLETED)
        t2 = Task(agent_name="agent-a", state=TaskState.FAILED)
        t3 = Task(agent_name="agent-b", state=TaskState.COMPLETED)
        client._tasks[t1.id] = t1
        client._tasks[t2.id] = t2
        client._tasks[t3.id] = t3

        # Filter by agent
        agent_a_tasks = client.get_local_tasks(agent_name="agent-a")
        assert len(agent_a_tasks) == 2

        # Filter by state
        completed = client.get_local_tasks(state=TaskState.COMPLETED)
        assert len(completed) == 2

        # Filter by both
        agent_a_completed = client.get_local_tasks(
            agent_name="agent-a", state=TaskState.COMPLETED
        )
        assert len(agent_a_completed) == 1


# ---------------------------------------------------------------------------
# Client — send_task validation (no HTTP)
# ---------------------------------------------------------------------------


class TestA2AClientValidation:
    @pytest.mark.asyncio
    async def test_send_task_unknown_agent(self):
        """Sending to undiscovered agent raises error."""
        discovery = AgentDiscovery()
        client = A2AClient(discovery=discovery)

        with pytest.raises(AgentNotFoundError):
            await client.send_task(
                agent_name="nonexistent",
                message="Do something",
            )

    @pytest.mark.asyncio
    async def test_send_task_invalid_skill(self):
        """Sending with invalid skill_id raises error."""
        discovery = AgentDiscovery()
        card = AgentCard(
            name="test-agent",
            description="Test",
            url="http://localhost:8100",
            skills=[
                AgentSkill(
                    id="real-skill",
                    name="Real Skill",
                    description="Exists",
                )
            ],
        )
        discovery.register_local(card)
        client = A2AClient(discovery=discovery)

        with pytest.raises(SkillNotFoundError):
            await client.send_task(
                agent_name="test-agent",
                message="Do something",
                skill_id="fake-skill",
            )
