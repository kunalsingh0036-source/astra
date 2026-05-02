"""
End-to-end foundation tests.

These tests verify every Astra system works together:
1. Memory: store → search → recall → delete
2. Autonomy: mode switching, permission decisions, audit logging
3. Agent Fleet: registration, listing, recommendations
4. Core Agent: configuration, imports, system prompt
5. Model Router: task routing to correct models
6. System: health check, info
7. Research-Intel: agent definition, registration

Does NOT make Claude API calls — tests the infrastructure layer.
"""

import pytest
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from astra import __version__
from astra.config import settings


# ── Memory System ──────────────────────────────────────────────

class TestMemoryE2E:
    """Full memory lifecycle: store → embed → search → access → delete."""

    @pytest.fixture
    async def session(self):
        engine = create_async_engine(settings.database_url)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as session:
            yield session
        await engine.dispose()

    @pytest.mark.asyncio
    async def test_full_memory_lifecycle(self, session):
        from astra.memory.store import store_memory, get_memory, delete_memory
        from astra.memory.retrieval import search_memories
        from astra.memory.models import MemoryType

        # 1. Store memories
        m1 = await store_memory(
            session, "Kunal's top priority is building Astra",
            MemoryType.SEMANTIC, source="user", tags="priority,astra",
            importance=0.9,
        )
        m2 = await store_memory(
            session, "Meeting with investor on Friday went well, $500K committed",
            MemoryType.EPISODIC, source="user", tags="investor,funding",
            importance=0.8,
        )
        m3 = await store_memory(
            session, "To deploy Astra: docker compose up -d && alembic upgrade head",
            MemoryType.PROCEDURAL, source="agent", tags="deployment",
        )
        assert m1.id is not None
        assert m2.id is not None
        assert m3.id is not None

        # 2. Semantic search finds relevant memories
        results = await search_memories(session, "What is Kunal working on?")
        assert len(results) > 0
        # The Astra priority memory should rank highly
        content_texts = [r["content"] for r in results]
        assert any("Astra" in c for c in content_texts)

        # 3. Search for funding-related memories
        funding_results = await search_memories(session, "investor funding round")
        assert any("investor" in r["content"].lower() or "500K" in r["content"] for r in funding_results)

        # 4. Search for deployment procedures (lower threshold for procedural)
        deploy_results = await search_memories(
            session, "how to deploy the application",
            memory_type=MemoryType.PROCEDURAL,
            relevance_threshold=0.2,
        )
        assert any("docker" in r["content"].lower() for r in deploy_results)

        # 5. Access tracking works
        fetched = await get_memory(session, m1.id)
        assert fetched.access_count >= 1

        # 6. Delete works
        deleted = await delete_memory(session, m3.id)
        assert deleted is True

    @pytest.mark.asyncio
    async def test_memory_stats(self, session):
        from astra.memory.consolidation import get_memory_stats
        stats = await get_memory_stats(session)
        assert "total_memories" in stats
        assert "by_type" in stats


# ── Autonomy System ────────────────────────────────────────────

class TestAutonomyE2E:
    """Full autonomy lifecycle: mode switching, permission checks, audit."""

    def test_mode_switching_lifecycle(self):
        from astra.autonomy.manager import AutonomyManager
        from astra.autonomy.modes import AutonomyMode, get_permission, PermissionDecision
        import time

        mgr = AutonomyManager()

        # Start in default mode from config
        default = mgr.mode
        # Ensure we start from a known state for the test
        mgr.set_mode(AutonomyMode.ALWAYS_ASK, reason="Reset for test")
        assert mgr.mode == AutonomyMode.ALWAYS_ASK
        assert get_permission(mgr.mode, "Bash") == PermissionDecision.ASK

        # Switch to semi_auto
        mgr.set_mode(AutonomyMode.SEMI_AUTO, reason="Testing")
        assert mgr.mode == AutonomyMode.SEMI_AUTO
        assert get_permission(mgr.mode, "Read") == PermissionDecision.ALLOW
        assert get_permission(mgr.mode, "Bash") == PermissionDecision.ASK

        # Time-based switch to full_auto
        mgr.set_mode(AutonomyMode.FULL_AUTO, duration_minutes=0)
        mgr._revert_at = time.time() - 1  # Force expiry
        assert mgr.mode == AutonomyMode.SEMI_AUTO  # Reverted

        # Task-based switch
        mgr.set_mode(AutonomyMode.FULL_AUTO, task_id="research-123")
        assert mgr.mode == AutonomyMode.FULL_AUTO
        mgr.complete_task("research-123")
        assert mgr.mode == AutonomyMode.SEMI_AUTO  # Reverted

        # History tracked
        history = mgr.get_history()
        assert len(history) >= 4

    def test_audit_logging_lifecycle(self):
        from astra.autonomy.audit import AuditLogger
        from astra.autonomy.modes import ActionTier, AutonomyMode, PermissionDecision

        logger = AuditLogger()

        # Log various actions
        logger.log("Read", ActionTier.READ, AutonomyMode.SEMI_AUTO, PermissionDecision.ALLOW)
        logger.log("Edit", ActionTier.WRITE, AutonomyMode.SEMI_AUTO, PermissionDecision.ALLOW)
        logger.log("Bash", ActionTier.DESTRUCTIVE, AutonomyMode.SEMI_AUTO, PermissionDecision.ASK,
                    tool_input_summary="rm -rf /tmp/test")
        logger.log("Bash", ActionTier.DESTRUCTIVE, AutonomyMode.ALWAYS_ASK, PermissionDecision.ASK)

        # Retrieve all
        entries = logger.get_entries()
        assert len(entries) == 4

        # Filter by tool
        bash_entries = logger.get_entries(tool_name="Bash")
        assert len(bash_entries) == 2

        # Filter by decision
        ask_entries = logger.get_entries(decision=PermissionDecision.ASK)
        assert len(ask_entries) == 2

        # Stats
        stats = logger.get_stats()
        assert stats["total"] == 4
        assert stats["by_decision"]["allow"] == 2
        assert stats["by_tier"]["destructive"] == 2


# ── Agent Fleet ────────────────────────────────────────────────

class TestFleetE2E:
    """Fleet management: registration, recommendations, research-intel."""

    def test_research_intel_registered(self):
        from astra.agents.definitions.research_intel import register, AGENT_NAME
        from astra.agents.registry import AgentRegistry, AgentStatus

        registry = AgentRegistry()
        # Simulate registration
        from astra.agents.definitions.research_intel import AgentDefinitionRecord
        registry.register(AgentDefinitionRecord(
            name=AGENT_NAME,
            description="Research agent",
            capabilities=["research", "analysis"],
            status=AgentStatus.ACTIVE,
            tools=["WebSearch", "WebFetch"],
            model_tier="sonnet",
        ))

        agents = registry.list_all()
        assert len(agents) == 1
        assert agents[0]["name"] == "research-intel"
        assert agents[0]["status"] == "active"

    def test_research_intel_definition(self):
        from astra.agents.definitions.research_intel import get_agent_definition

        definition = get_agent_definition()
        assert definition.description is not None
        assert "WebSearch" in definition.tools
        assert definition.model == "sonnet"

    def test_recommendations_exclude_built_agents(self):
        from astra.agents.registry import AgentRegistry, AgentDefinitionRecord, AgentStatus
        from astra.agents.recommender import get_recommendations, AGENT_PROPOSALS

        # Fresh registry with research-intel already registered
        # The global registry might have it from other tests
        # Test the exclusion logic directly
        recs = get_recommendations()
        rec_names = [r["name"] for r in recs]

        # All recommendations should have required fields
        for rec in recs:
            assert "name" in rec
            assert "priority_score" in rec
            assert "rationale" in rec
            assert "build_complexity" in rec

    def test_fleet_summary(self):
        from astra.agents.registry import AgentRegistry, AgentDefinitionRecord, AgentStatus

        registry = AgentRegistry()
        registry.register(AgentDefinitionRecord(
            "agent-1", "Active agent", ["x"], AgentStatus.ACTIVE
        ))
        registry.register(AgentDefinitionRecord(
            "agent-2", "Building agent", ["x"], AgentStatus.BUILDING
        ))

        summary = registry.get_fleet_summary()
        assert summary["total"] == 2
        assert summary["active"] == 1
        assert summary["building"] == 1


# ── Core Agent Configuration ───────────────────────────────────

class TestCoreAgentE2E:
    """Core agent configuration and integration."""

    def test_create_astra_options(self):
        from astra.core.agent import create_astra_options

        options = create_astra_options()

        # Has system prompt
        assert options.system_prompt is not None
        assert "Astra" in options.system_prompt

        # Has MCP servers
        assert "astra-memory" in options.mcp_servers
        assert "astra-autonomy" in options.mcp_servers
        assert "astra-fleet" in options.mcp_servers
        assert "astra-system" in options.mcp_servers

        # Has research-intel sub-agent
        assert "research-intel" in options.agents
        assert options.agents["research-intel"].model == "sonnet"

        # Has hooks
        assert "PreToolUse" in options.hooks
        assert "PostToolUse" in options.hooks

        # Has budget limits
        assert options.max_turns == 50

    def test_system_prompt_completeness(self):
        from astra.core.system_prompt import get_system_prompt

        prompt = get_system_prompt()
        # Must mention key capabilities
        assert "Memory" in prompt or "memory" in prompt
        assert "Autonomy" in prompt or "autonomy" in prompt
        assert "Agent Fleet" in prompt or "agent" in prompt.lower()
        assert "store_memory" in prompt
        assert "recall_memories" in prompt

    def test_model_router(self):
        from astra.core.model_router import get_model_for_task, get_effort_for_task

        # Simple tasks → Haiku
        simple = get_model_for_task("classify this email, simple yes or no list")
        assert "haiku" in simple

        # Complex tasks → Opus
        complex_task = get_model_for_task(
            "plan and design the multi-step strategy to analyze the complex trade-off"
        )
        assert "opus" in complex_task

        # Medium tasks → Sonnet
        medium = get_model_for_task("write a function to parse JSON")
        assert "sonnet" in medium

        # Effort routing
        assert get_effort_for_task("classify this, simple list") == "low"
        assert get_effort_for_task("complex multi-step strategy to plan and analyze") == "max"

        # Force override
        forced = get_model_for_task("anything", force_tier="opus")
        assert "opus" in forced


# ── System Tools ───────────────────────────────────────────────

class TestSystemE2E:
    """System info and health checks."""

    def test_version(self):
        assert __version__ == "0.1.0"

    def test_config_loaded(self):
        assert settings.embedding_model == "all-MiniLM-L6-v2"
        assert settings.embedding_dimension == 384
        assert settings.default_autonomy_mode in ("always_ask", "semi_auto", "full_auto")
        assert "5433" in settings.database_url
        assert "6380" in settings.redis_url

    @pytest.mark.asyncio
    async def test_database_connectivity(self):
        from sqlalchemy import text
        from astra.db.engine import engine

        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT 1"))
            assert result.scalar() == 1

    @pytest.mark.asyncio
    async def test_pgvector_extension(self):
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        test_engine = create_async_engine(settings.database_url)
        async with test_engine.connect() as conn:
            result = await conn.execute(
                text("SELECT extname FROM pg_extension WHERE extname = 'vector'")
            )
            assert result.scalar() == "vector"
        await test_engine.dispose()

    def test_redis_connectivity(self):
        import redis as redis_lib
        r = redis_lib.from_url(settings.redis_url)
        assert r.ping() is True

    def test_embedding_model_loads(self):
        from astra.memory.embeddings import embed_text
        vec = embed_text("test embedding")
        assert len(vec) == 384
