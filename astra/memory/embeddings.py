"""
Text-to-vector embedding generation using sentence-transformers.

Runs locally — no API calls, no cost, no latency.
Model: all-MiniLM-L6-v2 (384 dimensions, fast, good quality for semantic search).

The embedding model is loaded once and reused. If quality isn't sufficient,
swap the model name in config.py — the interface stays the same.
"""

from functools import lru_cache

import numpy as np
from sentence_transformers import SentenceTransformer

from astra.config import settings


@lru_cache(maxsize=1)
def _get_model() -> SentenceTransformer:
    """Load the embedding model once and cache it."""
    return SentenceTransformer(settings.embedding_model)


def embed_text(text: str) -> list[float]:
    """Convert text to a vector embedding.

    Args:
        text: The text to embed.

    Returns:
        A list of floats (the embedding vector).
    """
    model = _get_model()
    embedding = model.encode(text, normalize_embeddings=True)
    return embedding.tolist()


def embed_batch(texts: list[str]) -> list[list[float]]:
    """Convert multiple texts to vector embeddings in a single batch.

    More efficient than calling embed_text in a loop because
    sentence-transformers batches the computation.

    Args:
        texts: List of texts to embed.

    Returns:
        List of embedding vectors.
    """
    model = _get_model()
    embeddings = model.encode(texts, normalize_embeddings=True)
    return embeddings.tolist()


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors.

    Used for client-side re-ranking when needed.
    pgvector handles this in SQL for the primary search path.
    """
    a_np = np.array(a)
    b_np = np.array(b)
    return float(np.dot(a_np, b_np) / (np.linalg.norm(a_np) * np.linalg.norm(b_np)))
