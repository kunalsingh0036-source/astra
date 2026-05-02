"""
Semantic memory retrieval via pgvector.

This is how Astra finds relevant memories: convert the query to a vector,
then find the memories whose vectors are closest (cosine similarity).

Results are re-ranked using the importance scoring system which combines
vector similarity with recency and access frequency.
"""

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from astra.config import settings
from astra.memory.embeddings import embed_text
from astra.memory.importance import compute_importance
from astra.memory.models import Memory, MemoryType


async def search_memories(
    session: AsyncSession,
    query: str,
    memory_type: MemoryType | None = None,
    top_k: int | None = None,
    relevance_threshold: float | None = None,
) -> list[dict]:
    """Search memories by semantic similarity.

    Converts the query to a vector, finds the closest memories via pgvector,
    then re-ranks using the importance scoring system.

    Args:
        session: Async database session.
        query: Natural language search query.
        memory_type: Optional filter by memory type.
        top_k: Max number of results (default from config).
        relevance_threshold: Minimum cosine similarity (default from config).

    Returns:
        List of dicts with memory data and scores, sorted by importance.
    """
    top_k = top_k or settings.memory_top_k
    relevance_threshold = relevance_threshold or settings.memory_relevance_threshold

    query_embedding = embed_text(query)

    # Fetch more candidates than needed for re-ranking
    candidate_limit = top_k * 3

    # pgvector cosine distance: 1 - cosine_similarity
    # So we compute similarity as: 1 - cosine_distance
    stmt = (
        select(
            Memory,
            (1 - Memory.embedding.cosine_distance(query_embedding)).label(
                "similarity"
            ),
        )
        .order_by(Memory.embedding.cosine_distance(query_embedding))
        .limit(candidate_limit)
    )

    if memory_type:
        stmt = stmt.where(Memory.memory_type == memory_type)

    result = await session.execute(stmt)
    rows = result.all()

    # Re-rank with importance scoring
    scored_results = []
    for memory, similarity in rows:
        if similarity < relevance_threshold:
            continue

        importance = compute_importance(
            relevance=similarity,
            created_at=memory.created_at,
            access_count=memory.access_count,
            last_accessed=memory.last_accessed,
            base_importance=memory.importance,
        )

        scored_results.append(
            {
                "id": memory.id,
                "content": memory.content,
                "memory_type": memory.memory_type.value,
                "source": memory.source,
                "tags": memory.tags,
                "similarity": round(similarity, 4),
                "importance": round(importance, 4),
                "access_count": memory.access_count,
                "created_at": memory.created_at.isoformat(),
            }
        )

    # Sort by importance (descending) and take top_k
    scored_results.sort(key=lambda x: x["importance"], reverse=True)
    results = scored_results[:top_k]

    # Record access for returned memories
    if results:
        memory_ids = [r["id"] for r in results]
        for memory, _ in rows:
            if memory.id in memory_ids:
                memory.access_count += 1
        await session.commit()

    return results
