"""
Threshold calibration tests for memory recall.

Why this exists: the relevance threshold in config.py was set to 0.5
for months while the embedding model (all-MiniLM-L6-v2) produces
similarities in the 0.25-0.40 range for legitimate matches at
typical content lengths. The result was the agent saying
"No relevant memories found" while the right answer sat in the DB.

These tests catch that bug class — if anyone re-tunes the threshold
above the empirical noise floor, or swaps to an embedding model with
a different similarity distribution without retuning, these fail
fast.

Distinct from test_store_and_retrieval.py which exercises the full
DB pipeline. These tests work on raw embeddings — fast, no DB
required, run in CI without infrastructure.
"""

from astra.config import settings
from astra.memory.embeddings import embed_text, cosine_similarity


# Realistic query/content pairs taken from production memory examples.
# Each row: (query a user might actually ask, the memory content that
# SHOULD match). If similarity is below threshold, the agent fails
# to recall information that's right there in the DB.
SHOULD_MATCH_PAIRS = [
    # The real-world failure that motivated this test.
    (
        "Where were we on our Studio 375 analysis",
        (
            "Top Studios website reference: 375.studio/en - Italian creative "
            "agency. Task: Study design patterns, animations, pages, "
            "structure as reference for our own Top Studios website."
        ),
    ),
    (
        "Studio 375",
        (
            "Kunal previously shared https://375.studio/en and requested "
            "an in-depth analysis covering styles, animations, and site "
            "structure."
        ),
    ),
    # Generic short-query case — short queries are exactly where 0.5
    # threshold over-rejects.
    (
        "what's my morning routine",
        (
            "Kunal does squash training in the morning, then heads to the "
            "office at 9am. Briefings should land before 7am IST."
        ),
    ),
    (
        "HelmTech revenue",
        (
            "HelmTech is a sales-outreach platform. Current ARR ~$2M, "
            "target $20-30M by April 2027. Chinmay Goyal is CTO."
        ),
    ),
]


def test_threshold_below_legitimate_match_floor() -> None:
    """The configured threshold must be at or below the lowest-
    similarity legitimate match across our calibration set.

    If this test fails, the agent will return "No relevant memories
    found" for valid queries against memories that actually exist.
    """
    legit_sims: list[tuple[str, str, float]] = []
    for query, content in SHOULD_MATCH_PAIRS:
        sim = cosine_similarity(embed_text(query), embed_text(content))
        legit_sims.append((query, content[:60], sim))

    floor = min(s for _, _, s in legit_sims)
    assert settings.memory_relevance_threshold <= floor, (
        f"memory_relevance_threshold={settings.memory_relevance_threshold} "
        f"is above the empirical legitimate-match floor ({floor:.3f}). "
        f"Real queries will return 'No relevant memories found' even when "
        f"the right answer is in the DB. Lower the threshold or recalibrate "
        f"the embedding model.\n"
        f"Per-pair similarities:\n"
        + "\n".join(f"  {sim:.3f}  {q!r} → {c!r}" for q, c, sim in legit_sims)
    )


def test_threshold_above_noise_floor() -> None:
    """The threshold must still filter genuine noise — pairs where the
    query has nothing to do with the content. Otherwise recall_memories
    returns junk and the agent's reasoning degrades.
    """
    noise_pairs = [
        ("Studio 375 analysis", "The capital of France is Paris."),
        ("HelmTech revenue", "Recipe for chocolate chip cookies: butter, flour, sugar."),
        ("morning routine", "Postgres pool_pre_ping prevents connection-refused errors."),
    ]
    noise_sims = [
        cosine_similarity(embed_text(q), embed_text(c))
        for q, c in noise_pairs
    ]
    ceiling = max(noise_sims)
    assert settings.memory_relevance_threshold > ceiling, (
        f"memory_relevance_threshold={settings.memory_relevance_threshold} "
        f"is at or below the noise ceiling ({ceiling:.3f}). The agent will "
        f"surface unrelated memories and degrade its reasoning."
    )
