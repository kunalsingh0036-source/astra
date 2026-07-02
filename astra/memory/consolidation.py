"""
Memory consolidation — periodic maintenance of Astra's memory.

Over time, memories accumulate. Consolidation:
1. Prunes low-importance working memories older than a threshold
2. Decays importance of unaccessed memories over time
3. Identifies near-duplicate memories and merges them
4. Generates summary memories from clusters of related episodic memories

This runs as a scheduled task via Celery, not on every query.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from itertools import groupby

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from astra.config import settings
from astra.memory.embeddings import embed_text
from astra.memory.models import Memory, MemoryType

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Step 1: Prune stale working memory
# ---------------------------------------------------------------------------


async def prune_stale_working_memory(
    session: AsyncSession,
    max_age_hours: int = 24,
) -> int:
    """Delete working memories older than max_age_hours.

    Working memory is session-scoped context that shouldn't persist long-term.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    result = await session.execute(
        delete(Memory).where(
            Memory.memory_type == MemoryType.WORKING,
            Memory.created_at < cutoff,
        )
    )
    await session.commit()
    return result.rowcount


# ---------------------------------------------------------------------------
# Step 2: Decay importance of unaccessed memories
# ---------------------------------------------------------------------------


async def decay_importance(
    session: AsyncSession,
    decay_factor: float | None = None,
    min_age_days: int = 7,
) -> int:
    """Reduce importance of memories that haven't been accessed.

    Memories with access_count == 0 and older than min_age_days get their
    importance multiplied by decay_factor. After ~30 weeks of zero access,
    a memory with base importance 0.5 drops to ~0.1 (the prune threshold).

    Never decays procedural memories — those are explicitly stored procedures.
    """
    if decay_factor is None:
        decay_factor = settings.consolidation_decay_factor

    cutoff = datetime.now(timezone.utc) - timedelta(days=min_age_days)
    result = await session.execute(
        update(Memory)
        .where(
            Memory.access_count == 0,
            Memory.created_at < cutoff,
            Memory.memory_type != MemoryType.PROCEDURAL,
            Memory.importance > settings.consolidation_prune_threshold,
        )
        .values(importance=Memory.importance * decay_factor)
    )
    await session.commit()
    return result.rowcount


# ---------------------------------------------------------------------------
# Step 3: Prune low-importance memories
# ---------------------------------------------------------------------------


async def prune_low_importance(
    session: AsyncSession,
    threshold: float | None = None,
    min_age_days: int | None = None,
) -> int:
    """Delete memories with very low importance that haven't been accessed.

    Only targets memories older than min_age_days to avoid pruning
    things that just haven't had a chance to be useful yet.
    """
    if threshold is None:
        threshold = settings.consolidation_prune_threshold
    if min_age_days is None:
        min_age_days = settings.consolidation_min_age_days

    cutoff = datetime.now(timezone.utc) - timedelta(days=min_age_days)
    result = await session.execute(
        delete(Memory).where(
            Memory.importance < threshold,
            Memory.access_count == 0,
            Memory.created_at < cutoff,
            Memory.memory_type != MemoryType.PROCEDURAL,
        )
    )
    await session.commit()
    return result.rowcount


# ---------------------------------------------------------------------------
# Step 4: Find and merge near-duplicates
# ---------------------------------------------------------------------------


async def find_near_duplicates(
    session: AsyncSession,
    similarity_threshold: float = 0.95,
    limit: int = 100,
) -> list[tuple[Memory, Memory, float]]:
    """Find pairs of memories that are near-duplicates.

    Returns pairs (memory_a, memory_b, similarity) where similarity
    exceeds the threshold. These can be manually reviewed or auto-merged.

    Note: This is O(n²) and should only run during scheduled consolidation,
    not on every query. For large memory stores, consider batching.
    """
    memories = (await session.execute(select(Memory).limit(limit * 2))).scalars().all()

    duplicates = []
    for i, a in enumerate(memories):
        if a.embedding is None:
            continue
        for b in memories[i + 1 :]:
            if b.embedding is None:
                continue
            # Proper cosine similarity: dot(a,b) / (||a|| * ||b||)
            # sentence-transformers produces normalized vectors, so this
            # simplifies to just the dot product
            dot = sum(x * y for x, y in zip(a.embedding, b.embedding))
            norm_a = sum(x * x for x in a.embedding) ** 0.5
            norm_b = sum(x * x for x in b.embedding) ** 0.5
            denom = norm_a * norm_b
            sim = dot / denom if denom > 0 else 0.0
            if sim > similarity_threshold:
                duplicates.append((a, b, sim))

    return duplicates


async def merge_duplicates(
    session: AsyncSession,
    similarity_threshold: float = 0.95,
) -> int:
    """Find near-duplicate memories and merge them.

    For each duplicate pair:
    - Keep the memory with higher importance (or more accesses as tiebreaker)
    - Combine tags from both (union)
    - Delete the weaker duplicate

    Returns count of merged (deleted) memories.
    """
    duplicates = await find_near_duplicates(session, similarity_threshold)
    merged_count = 0
    deleted_ids = set()

    for mem_a, mem_b, sim in duplicates:
        # Skip if either was already deleted in this pass
        if mem_a.id in deleted_ids or mem_b.id in deleted_ids:
            continue

        # Decide which to keep: higher importance wins, access_count breaks ties
        if (mem_a.importance, mem_a.access_count) >= (mem_b.importance, mem_b.access_count):
            keep, discard = mem_a, mem_b
        else:
            keep, discard = mem_b, mem_a

        # Merge tags (union of comma-separated lists)
        tags_keep = set((keep.tags or "").split(",")) if keep.tags else set()
        tags_discard = set((discard.tags or "").split(",")) if discard.tags else set()
        merged_tags = tags_keep | tags_discard
        merged_tags.discard("")  # remove empty strings
        if merged_tags:
            keep.tags = ",".join(sorted(merged_tags))

        # Accumulate access count
        keep.access_count += discard.access_count

        # Boost importance slightly (we confirmed this info from two sources)
        keep.importance = min(1.0, keep.importance + 0.05)

        # Delete the duplicate
        await session.delete(discard)
        deleted_ids.add(discard.id)
        merged_count += 1

        logger.info(
            f"Merged memory {discard.id} into {keep.id} (sim={sim:.3f})"
        )

    if merged_count > 0:
        await session.commit()

    return merged_count


# ---------------------------------------------------------------------------
# Step 5: Summarize old episodic memory clusters
# ---------------------------------------------------------------------------


async def summarize_old_episodic_clusters(
    session: AsyncSession,
    min_age_days: int | None = None,
    min_cluster_size: int = 5,
    max_clusters: int | None = None,
) -> int:
    """Group old episodic memories by week, summarize each cluster.

    For each week with enough memories:
    - Call Claude Haiku to generate a one-paragraph summary
    - Store as a new semantic memory with source="consolidation"
    - Delete the original episodic memories

    Returns count of summaries generated.
    """
    if min_age_days is None:
        min_age_days = settings.consolidation_summary_min_age_days
    if max_clusters is None:
        max_clusters = settings.consolidation_max_clusters

    cutoff = datetime.now(timezone.utc) - timedelta(days=min_age_days)

    result = await session.execute(
        select(Memory)
        .where(
            Memory.memory_type == MemoryType.EPISODIC,
            Memory.created_at < cutoff,
        )
        .order_by(Memory.created_at.asc())
    )
    old_episodic = list(result.scalars().all())

    if not old_episodic:
        return 0

    # Group by ISO calendar week
    def week_key(mem: Memory) -> str:
        dt = mem.created_at
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        year, week, _ = dt.isocalendar()
        return f"{year}-W{week:02d}"

    clusters: dict[str, list[Memory]] = {}
    for mem in old_episodic:
        key = week_key(mem)
        clusters.setdefault(key, []).append(mem)

    # Filter to clusters large enough to summarize
    eligible = {k: v for k, v in clusters.items() if len(v) >= min_cluster_size}
    if not eligible:
        return 0

    summaries_created = 0
    import anthropic

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    for week_label, memories in sorted(eligible.items())[:max_clusters]:
        # Build the content for summarization
        memory_texts = [
            f"- [{m.created_at.strftime('%Y-%m-%d')}] {m.content}"
            for m in memories
        ]
        prompt = (
            f"Summarize these {len(memories)} events from week {week_label} "
            f"into one concise paragraph. Preserve key facts, decisions, and outcomes. "
            f"Write in first person (as Kunal's AI assistant recalling what happened).\n\n"
            + "\n".join(memory_texts)
        )

        try:
            response = client.messages.create(
                model=settings.model_haiku,
                max_tokens=300,
                messages=[{"role": "user", "content": prompt}],
            )
            summary_text = response.content[0].text

            # Collect tags from all memories in the cluster
            all_tags = set()
            for m in memories:
                if m.tags:
                    all_tags.update(m.tags.split(","))
            all_tags.discard("")
            all_tags.add("consolidated")
            all_tags.add(week_label)

            # Store as a new semantic memory
            embedding = await asyncio.to_thread(embed_text, summary_text)
            summary_memory = Memory(
                content=f"[Week {week_label} Summary] {summary_text}",
                memory_type=MemoryType.SEMANTIC,
                source="consolidation",
                tags=",".join(sorted(all_tags)),
                embedding=embedding,
                importance=0.6,
            )
            session.add(summary_memory)

            # Delete the original episodic memories
            for m in memories:
                await session.delete(m)

            await session.commit()
            summaries_created += 1

            logger.info(
                f"Consolidated {len(memories)} memories from {week_label} "
                f"into summary (id={summary_memory.id})"
            )

        except Exception as e:
            logger.error(f"Failed to summarize cluster {week_label}: {e}")
            continue

    return summaries_created


# ---------------------------------------------------------------------------
# Step 6: Stats
# ---------------------------------------------------------------------------


async def get_memory_stats(session: AsyncSession) -> dict:
    """Get statistics about the memory store.

    Useful for the dashboard and for Astra's self-awareness.
    """
    total = (await session.execute(select(Memory))).scalars().all()

    stats = {
        "total_memories": len(total),
        "by_type": {},
        "by_source": {},
        "avg_importance": 0.0,
        "avg_access_count": 0.0,
    }

    if not total:
        return stats

    for mem in total:
        t = mem.memory_type.value
        stats["by_type"][t] = stats["by_type"].get(t, 0) + 1

        s = mem.source
        stats["by_source"][s] = stats["by_source"].get(s, 0) + 1

    stats["avg_importance"] = round(
        sum(m.importance for m in total) / len(total), 3
    )
    stats["avg_access_count"] = round(
        sum(m.access_count for m in total) / len(total), 1
    )

    return stats


# ---------------------------------------------------------------------------
# Orchestrator — runs all consolidation steps in sequence
# ---------------------------------------------------------------------------


async def run_full_consolidation(session: AsyncSession) -> dict:
    """Run the complete memory consolidation pipeline.

    Steps:
    1. Prune stale working memory (fast)
    2. Decay importance of unaccessed memories (fast)
    3. Prune low-importance memories (fast)
    4. Merge near-duplicates (moderate)
    5. Summarize old episodic clusters (slow, uses Claude API)

    Returns a report dict with counts from each step.
    """
    logger.info("Starting full memory consolidation...")

    report = {}

    # Step 1
    report["working_pruned"] = await prune_stale_working_memory(session)
    logger.info(f"  Step 1: Pruned {report['working_pruned']} stale working memories")

    # Step 2
    report["importance_decayed"] = await decay_importance(session)
    logger.info(f"  Step 2: Decayed importance on {report['importance_decayed']} memories")

    # Step 3
    report["low_importance_pruned"] = await prune_low_importance(session)
    logger.info(f"  Step 3: Pruned {report['low_importance_pruned']} low-importance memories")

    # Step 4
    report["duplicates_merged"] = await merge_duplicates(session)
    logger.info(f"  Step 4: Merged {report['duplicates_merged']} duplicate memories")

    # Step 5
    report["clusters_summarized"] = await summarize_old_episodic_clusters(session)
    logger.info(f"  Step 5: Summarized {report['clusters_summarized']} episodic clusters")

    # Get final stats
    report["final_stats"] = await get_memory_stats(session)

    logger.info(
        f"Consolidation complete: {report['final_stats']['total_memories']} memories remaining"
    )

    return report
