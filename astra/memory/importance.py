"""
Memory importance scoring.

Determines which memories are most relevant when multiple matches are found.
Combines three signals:

1. Relevance (cosine similarity from pgvector) — how semantically close is this
   memory to the query?
2. Recency — how recently was this memory created or accessed? Recent memories
   are usually more relevant.
3. Access frequency — memories that are accessed often are probably important.

The final score is a weighted combination of these three signals.
"""

import math
from datetime import datetime, timezone


# Weights for combining signals (must sum to 1.0)
RELEVANCE_WEIGHT = 0.6
RECENCY_WEIGHT = 0.25
FREQUENCY_WEIGHT = 0.15

# Recency decay: half-life in days.
# After this many days, the recency score drops to 0.5.
RECENCY_HALF_LIFE_DAYS = 7.0


def compute_recency_score(
    created_at: datetime,
    last_accessed: datetime | None = None,
) -> float:
    """Score from 0 to 1 based on how recent the memory is.

    Uses exponential decay with a configurable half-life.
    The more recent timestamp (created or last accessed) is used.
    """
    now = datetime.now(timezone.utc)
    reference_time = last_accessed if last_accessed else created_at

    if reference_time.tzinfo is None:
        reference_time = reference_time.replace(tzinfo=timezone.utc)

    age_days = (now - reference_time).total_seconds() / 86400.0
    decay = math.exp(-math.log(2) * age_days / RECENCY_HALF_LIFE_DAYS)
    return max(0.0, min(1.0, decay))


def compute_frequency_score(access_count: int, max_count: int = 100) -> float:
    """Score from 0 to 1 based on how often the memory is accessed.

    Uses logarithmic scaling so the first few accesses matter most.
    """
    if access_count <= 0:
        return 0.0
    return min(1.0, math.log1p(access_count) / math.log1p(max_count))


def compute_importance(
    relevance: float,
    created_at: datetime,
    access_count: int,
    last_accessed: datetime | None = None,
    base_importance: float = 0.5,
) -> float:
    """Compute a final importance score for a memory.

    Args:
        relevance: Cosine similarity score (0 to 1) from vector search.
        created_at: When the memory was created.
        access_count: How many times the memory has been accessed.
        last_accessed: When the memory was last accessed.
        base_importance: The memory's intrinsic importance (set at creation).

    Returns:
        A float from 0 to 1 representing overall importance.
    """
    recency = compute_recency_score(created_at, last_accessed)
    frequency = compute_frequency_score(access_count)

    # Combine signals
    score = (
        RELEVANCE_WEIGHT * relevance
        + RECENCY_WEIGHT * recency
        + FREQUENCY_WEIGHT * frequency
    )

    # Boost by base importance (allows manual override)
    score = score * (0.5 + 0.5 * base_importance)

    return max(0.0, min(1.0, score))
