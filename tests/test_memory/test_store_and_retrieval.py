"""
Integration tests for the memory store and retrieval system.

Tests the full pipeline: embed → store → search → retrieve.
Requires PostgreSQL + pgvector running (via docker-compose).
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from astra.config import settings
from astra.db.engine import Base
from astra.memory.embeddings import embed_text, cosine_similarity
from astra.memory.importance import compute_importance, compute_recency_score
from astra.memory.models import Memory, MemoryType
from astra.memory.retrieval import search_memories
from astra.memory.store import (
    clear_working_memory,
    delete_memory,
    list_memories,
    store_memory,
)


@pytest.fixture
async def db_session():
    """Create a test database session."""
    engine = create_async_engine(settings.database_url)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with session_factory() as session:
        yield session

    await engine.dispose()


class TestEmbeddings:
    def test_embed_text_returns_correct_dimension(self):
        vec = embed_text("Hello world")
        assert len(vec) == settings.embedding_dimension
        assert all(isinstance(v, float) for v in vec)

    def test_similar_texts_have_high_similarity(self):
        v1 = embed_text("I love programming in Python")
        v2 = embed_text("Python is my favorite programming language")
        sim = cosine_similarity(v1, v2)
        assert sim > 0.7, f"Expected high similarity, got {sim}"

    def test_dissimilar_texts_have_low_similarity(self):
        v1 = embed_text("I love programming in Python")
        v2 = embed_text("The weather is sunny today")
        sim = cosine_similarity(v1, v2)
        assert sim < 0.5, f"Expected low similarity, got {sim}"


class TestImportance:
    def test_recency_score_recent_is_high(self):
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        score = compute_recency_score(now)
        assert score > 0.9

    def test_importance_high_relevance_dominates(self):
        from datetime import datetime, timezone

        score = compute_importance(
            relevance=0.95,
            created_at=datetime.now(timezone.utc),
            access_count=0,
        )
        assert score > 0.5


class TestMemoryStore:
    @pytest.mark.asyncio
    async def test_store_and_retrieve(self, db_session):
        memory = await store_memory(
            session=db_session,
            content="Kunal prefers concise communication",
            memory_type=MemoryType.SEMANTIC,
            source="user",
            tags="preference,communication",
            importance=0.8,
        )

        assert memory.id is not None
        assert memory.content == "Kunal prefers concise communication"
        assert memory.memory_type == MemoryType.SEMANTIC
        assert len(memory.embedding) == settings.embedding_dimension

    @pytest.mark.asyncio
    async def test_semantic_search_finds_relevant(self, db_session):
        # Store a few memories
        await store_memory(
            db_session, "Python is the best language for AI", MemoryType.SEMANTIC
        )
        await store_memory(
            db_session, "The meeting with investors is on Friday", MemoryType.EPISODIC
        )
        await store_memory(
            db_session, "To deploy, run docker compose up", MemoryType.PROCEDURAL
        )

        # Search for something related to AI
        results = await search_memories(db_session, "artificial intelligence programming")
        assert len(results) > 0
        # The AI-related memory should rank highest
        assert "Python" in results[0]["content"] or "AI" in results[0]["content"]

    @pytest.mark.asyncio
    async def test_delete_memory(self, db_session):
        memory = await store_memory(
            db_session, "temporary fact", MemoryType.WORKING
        )
        deleted = await delete_memory(db_session, memory.id)
        assert deleted is True

        deleted_again = await delete_memory(db_session, memory.id)
        assert deleted_again is False

    @pytest.mark.asyncio
    async def test_list_memories_with_filter(self, db_session):
        await store_memory(db_session, "semantic memory", MemoryType.SEMANTIC)
        await store_memory(db_session, "working memory", MemoryType.WORKING)

        semantic = await list_memories(db_session, memory_type=MemoryType.SEMANTIC)
        assert all(m.memory_type == MemoryType.SEMANTIC for m in semantic)

    @pytest.mark.asyncio
    async def test_clear_working_memory(self, db_session):
        await store_memory(db_session, "session context 1", MemoryType.WORKING)
        await store_memory(db_session, "session context 2", MemoryType.WORKING)

        count = await clear_working_memory(db_session)
        assert count >= 2
