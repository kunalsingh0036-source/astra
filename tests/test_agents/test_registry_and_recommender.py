"""Tests for the agent fleet registry and recommender."""

from astra.agents.recommender import get_recommendations
from astra.agents.registry import (
    AgentDefinitionRecord,
    AgentRegistry,
    AgentStatus,
)


class TestRegistry:
    def test_register_and_list(self):
        registry = AgentRegistry()
        agent = AgentDefinitionRecord(
            name="test-agent",
            description="A test agent",
            capabilities=["testing"],
            status=AgentStatus.ACTIVE,
        )
        registry.register(agent)

        agents = registry.list_all()
        assert len(agents) == 1
        assert agents[0]["name"] == "test-agent"
        assert agents[0]["status"] == "active"

    def test_filter_by_status(self):
        registry = AgentRegistry()
        registry.register(
            AgentDefinitionRecord("a1", "Active agent", ["x"], AgentStatus.ACTIVE)
        )
        registry.register(
            AgentDefinitionRecord("a2", "Proposed agent", ["x"], AgentStatus.PROPOSED)
        )

        active = registry.list_all(status=AgentStatus.ACTIVE)
        assert len(active) == 1
        assert active[0]["name"] == "a1"

    def test_record_usage(self):
        registry = AgentRegistry()
        agent = AgentDefinitionRecord("a1", "Agent", ["x"], AgentStatus.ACTIVE)
        registry.register(agent)

        registry.record_usage("a1")
        registry.record_usage("a1")
        assert agent.usage_count == 2
        assert agent.last_used is not None

    def test_fleet_summary(self):
        registry = AgentRegistry()
        registry.register(
            AgentDefinitionRecord("a1", "Active", ["x"], AgentStatus.ACTIVE)
        )
        registry.register(
            AgentDefinitionRecord("a2", "Building", ["x"], AgentStatus.BUILDING)
        )
        registry.register(
            AgentDefinitionRecord("a3", "Proposed", ["x"], AgentStatus.PROPOSED)
        )

        summary = registry.get_fleet_summary()
        assert summary["total"] == 3
        assert summary["active"] == 1
        assert summary["building"] == 1
        assert summary["proposed"] == 1

    def test_get_nonexistent(self):
        registry = AgentRegistry()
        assert registry.get("nonexistent") is None


class TestRecommender:
    def test_returns_recommendations(self):
        recs = get_recommendations()
        assert len(recs) > 0
        assert all("name" in r for r in recs)
        assert all("priority_score" in r for r in recs)
        assert all("rationale" in r for r in recs)

    def test_sorted_by_priority(self):
        recs = get_recommendations()
        scores = [r["priority_score"] for r in recs]
        assert scores == sorted(scores, reverse=True)

    def test_research_intel_is_top_priority(self):
        recs = get_recommendations()
        assert recs[0]["name"] == "research-intel"

    def test_max_results(self):
        recs = get_recommendations(max_results=2)
        assert len(recs) <= 2
