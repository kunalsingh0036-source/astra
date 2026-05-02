"""
SQLAlchemy models for Astra's memory system.

Four memory types:
- Episodic: What happened, when, in what context (events, interactions, outcomes)
- Semantic: Facts, knowledge, preferences (timeless truths about the world or user)
- Procedural: How to do things (step-by-step procedures, workflows, commands)
- Working: Current session context (short-lived, cleared between sessions)

Each memory is stored with its vector embedding for semantic search via pgvector.
"""

import enum
from datetime import datetime, timezone

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, Enum, Float, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from astra.config import settings
from astra.db.engine import Base


class MemoryType(str, enum.Enum):
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"
    WORKING = "working"


class Memory(Base):
    """A single memory unit in Astra's long-term memory."""

    __tablename__ = "memories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Core content
    content: Mapped[str] = mapped_column(Text, nullable=False)
    memory_type: Mapped[MemoryType] = mapped_column(
        Enum(MemoryType), nullable=False, index=True
    )

    # Categorization
    source: Mapped[str] = mapped_column(
        String(100), nullable=False, default="user"
    )  # user, agent, system
    tags: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # comma-separated tags

    # Vector embedding for semantic search
    embedding: Mapped[list] = mapped_column(
        Vector(settings.embedding_dimension), nullable=False
    )

    # Importance scoring
    importance: Mapped[float] = mapped_column(Float, default=0.5)
    access_count: Mapped[int] = mapped_column(Integer, default=0)
    last_accessed: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Index for vector similarity search (HNSW — no training data required)
    __table_args__ = (
        Index(
            "ix_memories_embedding",
            embedding,
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    def __repr__(self) -> str:
        preview = self.content[:50] + "..." if len(self.content) > 50 else self.content
        return f"<Memory(id={self.id}, type={self.memory_type.value}, content='{preview}')>"
