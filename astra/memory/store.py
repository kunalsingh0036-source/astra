"""
Memory store — CRUD operations for Astra's long-term memory.

All database operations are async via SQLAlchemy 2.0 async sessions.
"""

from datetime import datetime, timezone

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from astra.memory.embeddings import embed_text
from astra.memory.models import Memory, MemoryType


async def store_memory(
    session: AsyncSession,
    content: str,
    memory_type: MemoryType,
    source: str = "user",
    tags: str | None = None,
    importance: float = 0.5,
) -> Memory:
    """Store a new memory with its embedding.

    Args:
        session: Async database session.
        content: The text content to remember.
        memory_type: Type of memory (episodic, semantic, procedural, working).
        source: Who created this memory (user, agent, system).
        tags: Optional comma-separated tags for filtering.
        importance: Base importance score (0 to 1).

    Returns:
        The created Memory object.
    """
    embedding = embed_text(content)

    memory = Memory(
        content=content,
        memory_type=memory_type,
        source=source,
        tags=tags,
        embedding=embedding,
        importance=importance,
    )

    session.add(memory)
    await session.commit()
    await session.refresh(memory)
    return memory


async def get_memory(session: AsyncSession, memory_id: int) -> Memory | None:
    """Get a single memory by ID and record the access."""
    result = await session.execute(select(Memory).where(Memory.id == memory_id))
    memory = result.scalar_one_or_none()

    if memory:
        memory.access_count += 1
        memory.last_accessed = datetime.now(timezone.utc)
        await session.commit()

    return memory


async def update_memory(
    session: AsyncSession,
    memory_id: int,
    content: str | None = None,
    tags: str | None = None,
    importance: float | None = None,
) -> Memory | None:
    """Update an existing memory. Re-embeds if content changes."""
    memory = await get_memory(session, memory_id)
    if not memory:
        return None

    if content is not None and content != memory.content:
        memory.content = content
        memory.embedding = embed_text(content)

    if tags is not None:
        memory.tags = tags

    if importance is not None:
        memory.importance = importance

    await session.commit()
    await session.refresh(memory)
    return memory


async def delete_memory(session: AsyncSession, memory_id: int) -> bool:
    """Delete a memory by ID. Returns True if deleted, False if not found."""
    result = await session.execute(delete(Memory).where(Memory.id == memory_id))
    await session.commit()
    return result.rowcount > 0


async def list_memories(
    session: AsyncSession,
    memory_type: MemoryType | None = None,
    source: str | None = None,
    tag: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[Memory]:
    """List memories with optional filtering.

    Args:
        memory_type: Filter by memory type.
        source: Filter by source (user, agent, system).
        tag: Filter by tag (substring match in comma-separated tags).
        limit: Max results.
        offset: Pagination offset.
    """
    query = select(Memory).order_by(Memory.created_at.desc())

    if memory_type:
        query = query.where(Memory.memory_type == memory_type)
    if source:
        query = query.where(Memory.source == source)
    if tag:
        query = query.where(Memory.tags.contains(tag))

    query = query.limit(limit).offset(offset)
    result = await session.execute(query)
    return list(result.scalars().all())


async def clear_working_memory(session: AsyncSession) -> int:
    """Clear all working memory (session context). Returns count deleted."""
    result = await session.execute(
        delete(Memory).where(Memory.memory_type == MemoryType.WORKING)
    )
    await session.commit()
    return result.rowcount
