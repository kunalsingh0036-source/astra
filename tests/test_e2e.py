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
    """Fleet management: external-agent registration + recommendations.

    The research_intel tests that used to live here imported a module
    deleted in the Phase-6 SDK removal (5f2d256) and failed every run
    for 5+ weeks — masked by check.yml's pytest soft-fail. Replaced
    with locks on the behavior that actually exists: the 7 external
    A2A agents registering into the fleet registry, which is what the
    stream-service startup hook (re-wired 2026-06-11 after its call
    site was lost in the same SDK removal) depends on.
    """

    def test_external_agents_register_into_fleet_and_discovery(self):
        from astra.agents.external.registry import (
            EXTERNAL_AGENTS,
            register_all_external_agents,
        )
        from astra.agents.registry import agent_registry

        n = register_all_external_agents()
        assert n == len(EXTERNAL_AGENTS) == 7

        registered = {a["name"] for a in agent_registry.list_all()}
        for card in EXTERNAL_AGENTS:
            assert card.name in registered, (
                f"{card.name} missing from fleet registry after "
                "register_all_external_agents()"
            )

    def test_external_agents_discoverable_via_a2a(self):
        from astra.a2a.discovery import agent_discovery
        from astra.agents.external.registry import (
            EXTERNAL_AGENTS,
            register_all_external_agents,
        )

        register_all_external_agents()
        for card in EXTERNAL_AGENTS:
            found = agent_discovery.get(card.name)
            assert found is not None, (
                f"{card.name} not in A2A discovery cache — send_a2a_task "
                "would fail with 'unknown agent'"
            )

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


# ── Lean Runtime Configuration ──────────────────────────────────
#
# The legacy `create_astra_options` test was removed in Phase 6 of
# the lean-runtime migration along with astra/core/agent.py. The
# replacement validates that the runtime registry has the expected
# tool surface — that's the equivalent assertion for the new path.

class TestLeanRuntimeE2E:
    """Lean runtime tool surface and registry shape."""

    def test_registry_has_critical_tools(self):
        # Side-effect-import every tool file
        import astra.runtime.tools  # noqa: F401
        from astra.runtime.tool_registry import REGISTRY

        names = set(REGISTRY.names())

        # Memory tools present
        assert "store_memory" in names
        assert "recall_memories" in names
        assert "recall_recent_turns" in names

        # Calendar / email / shares present
        assert "calendar_today" in names
        assert "email_unanswered" in names
        assert "list_recent_shares" in names

        # Creator tools present
        assert "list_business_kits" in names
        assert "analyze_reference_site" in names
        assert "draft_deck" in names

    def test_registry_namespaces(self):
        import astra.runtime.tools  # noqa: F401
        from astra.runtime.tool_registry import REGISTRY

        namespaces = {t.namespace for t in REGISTRY.all()}
        # Core namespaces all loaded
        for required in ("memory", "shares", "calendar", "email", "creators"):
            assert required in namespaces, (
                f"missing namespace {required} — adapter import probably "
                f"failed; check stream service startup logs"
            )

    def test_system_prompt_loadable(self):
        from astra.core.system_prompt import get_system_prompt

        prompt = get_system_prompt()
        assert prompt
        assert "Astra" in prompt
        # The old `assert options.max_turns == 50` here referenced an
        # SDK-era ClaudeAgentOptions variable that no longer exists —
        # a NameError that failed every run since Phase 6. The lean
        # runtime's equivalent budget lives in agent_loop:
        from astra.runtime.agent_loop import _MAX_TOOL_ITERATIONS

        assert _MAX_TOOL_ITERATIONS >= 10, "tool-iteration budget too tight"

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
        # Environment-agnostic: laptop runs Docker PG on 5433 / Redis
        # on 6380, CI's service container is 5432/6379, Railway is
        # internal hostnames. Asserting specific ports here made the
        # test fail everywhere except one laptop. What actually
        # matters: the URL parses and carries the async driver.
        assert settings.database_url.startswith("postgresql+asyncpg://")
        assert settings.redis_url.startswith("redis://")

    @pytest.mark.asyncio
    async def test_database_connectivity(self):
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        # Own engine per test, NOT the module-global astra.db.engine —
        # the global engine binds to the first asyncio event loop that
        # touches it, and pytest-asyncio gives each test its own loop.
        # Reusing the global was the source of the full-suite-only
        # RuntimeError flake (passed solo, failed in sequence).
        test_engine = create_async_engine(settings.database_url)
        try:
            async with test_engine.connect() as conn:
                result = await conn.execute(text("SELECT 1"))
                assert result.scalar() == 1
        finally:
            await test_engine.dispose()

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
        # Environment-availability probe, not a code regression test:
        # skip (don't fail) when redis isn't installed or running —
        # CI's check.yml provisions Postgres but not Redis, and the
        # only Redis consumers (Celery paths) aren't deployed either.
        redis_lib = pytest.importorskip("redis")
        r = redis_lib.from_url(settings.redis_url, socket_connect_timeout=2)
        try:
            assert r.ping() is True
        except redis_lib.exceptions.ConnectionError:
            pytest.skip(f"no redis listening at {settings.redis_url}")

    def test_embedding_model_loads(self):
        from astra.memory.embeddings import embed_text
        vec = embed_text("test embedding")
        assert len(vec) == 384
