"""
Post-turn memory extraction — belt-and-braces for the agent's own
proactive store_memory calls.

The agent's system prompt tells it to call store_memory aggressively
when the user shares URLs / preferences / decisions / etc. But agents
forget. Long contexts push rules out. Quick turns ("what time is it?")
feel below the threshold even when they aren't.

This module runs an async extraction pass at the end of every turn:
  1. Take the user prompt + assistant response.
  2. Ask Haiku to identify "facts the user shared that should be
     remembered for future sessions" (NOT the assistant's reasoning,
     NOT trivial chitchat).
  3. For each candidate, embed + search existing memories at high
     similarity (>=0.92). If a near-duplicate exists, skip.
  4. Store the survivors with source="auto_extracted" so they're
     audit-distinguishable from agent-driven and user-driven memories.

Cost: one Haiku call per turn (~$0.001) + one embedding per candidate.
Cheap enough to run unconditionally. Triggered fire-and-forget from
runner.py so the user sees the response immediately while extraction
runs in the background.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

import anthropic

from astra.config import settings
from astra.db.engine import async_session
from astra.memory.models import MemoryType
from astra.memory.retrieval import search_memories
from astra.memory.store import store_memory

logger = logging.getLogger(__name__)


_EXTRACT_MODEL = os.environ.get(
    "MEMORY_EXTRACT_MODEL", "claude-haiku-4-5"
)
_DEDUP_THRESHOLD = 0.92  # cosine similarity above which a candidate is a dup
_MAX_CANDIDATES_PER_TURN = 6  # cap to avoid storage spam from a single chatty turn


_EXTRACT_SYSTEM = """You scan one conversation turn between Kunal and his
personal AI agent (Astra) and identify FACTS THE USER SHARED that should
be remembered for future sessions.

Output STRICT JSON. No prose, no markdown fences. Schema:

{
  "memories": [
    {
      "content": "<one-sentence factual statement, third-person if about Kunal, first-person OK if quoting>",
      "tags": "<comma-separated short tags — e.g. 'url,reference,helmtech', 'preference,communication', 'decision,investor'>",
      "importance": <float 0-1>,
      "memory_type": "episodic" | "semantic"
    }
  ]
}

Rules:

1. **Store only what the USER shared or decided.** Skip anything that
   was just the agent's reasoning, web search results, or generated
   content. The signal is "did Kunal communicate something durable?"

2. **Specific triggers — store these:**
   - URLs / file paths / external references Kunal mentioned
     (`tags: url,reference,<topic>`, importance 0.7)
   - Preferences / rules / how-Kunal-wants-things-done
     (`tags: preference,<area>`, importance 0.7)
   - Decisions / commitments / deadlines Kunal stated
     (`tags: decision,<topic>`, importance 0.8)
   - Names of people / businesses / projects mentioned with context
     (`tags: person,<name>` or `tags: business,<slug>`, importance 0.6)
   - Substantive work Kunal asked Astra to do (study X, draft Y, find Z)
     (`tags: task,<topic>`, importance 0.7)
   - Corrections / "from now on" rules
     (`tags: rule,<area>`, importance 0.85)

3. **Skip:**
   - Trivial questions ("what time is it?")
   - Pure thinking-out-loud with no commitment
   - Anything Astra inferred or fetched from the web
   - Anything that's already obvious from Kunal's known compass
     (e.g. "Kunal runs HelmTech" — already known)
   - Pleasantries, acknowledgements, meta-questions about Astra itself
     ("why are you doing X?")

4. **memory_type:**
   - `episodic` — a specific event / interaction ("Kunal shared
     375.studio/en at 14:32 and asked for an in-depth analysis")
   - `semantic` — a general fact / preference / rule ("Kunal prefers
     Sonnet for drafts and Haiku for critique")

5. **importance** is a float 0-1. Defaults: 0.5 if unsure. Bump to 0.8+
   for explicit decisions / corrections / "remember this" statements.

6. **Maximum 6 memories per turn.** If you find more, pick the most
   durable / most likely to need recall later. A long turn doesn't
   mean many memories — most turns have 0 or 1.

7. **Empty result is the right answer when nothing is store-worthy.**
   Return `{"memories": []}` — better than fabricating low-value rows.

Return ONLY the JSON object. No preamble."""


def _get_anthropic_key() -> str:
    """Mirror of the resolution dance used in creators/_shared.py.
    Centralizing this is on the cleanup list; for now, duplicate
    so the memory module doesn't depend on the creators package."""
    key = settings.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    if key:
        return key
    env = Path(__file__).resolve().parents[2] / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("ANTHROPIC_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _strip_fences(text: str) -> str:
    """Strip ```json ... ``` wrappers if the model added them despite the rule."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    return text


async def _llm_extract(prompt: str, response: str) -> list[dict]:
    """Run the LLM extraction pass. Returns a list of candidate dicts.

    Empty list on any failure (including malformed JSON) — extraction
    is best-effort, never blocking the turn or surfacing errors.
    """
    key = _get_anthropic_key()
    if not key:
        return []

    user = (
        "<user-prompt>\n"
        f"{(prompt or '').strip()[:6000]}\n"
        "</user-prompt>\n\n"
        "<assistant-response>\n"
        f"{(response or '').strip()[:8000]}\n"
        "</assistant-response>\n\n"
        "Extract storeable user-facts now. Return JSON only."
    )

    try:
        client = anthropic.AsyncAnthropic(api_key=key)
        resp = await client.messages.create(
            model=_EXTRACT_MODEL,
            max_tokens=2000,
            system=_EXTRACT_SYSTEM,
            messages=[{"role": "user", "content": user}],
        )
    except Exception as e:
        logger.warning("[post-turn-extract] LLM call failed: %s", e)
        return []

    text_out = "\n".join(b.text for b in resp.content if hasattr(b, "text"))
    text_out = _strip_fences(text_out)

    try:
        parsed = json.loads(text_out)
    except json.JSONDecodeError:
        logger.warning("[post-turn-extract] JSON parse failed; head=%r", text_out[:200])
        return []

    candidates = parsed.get("memories") if isinstance(parsed, dict) else None
    if not isinstance(candidates, list):
        return []
    return candidates[:_MAX_CANDIDATES_PER_TURN]


async def _is_duplicate(content: str) -> bool:
    """Check if a candidate memory is a near-duplicate of an existing one.

    Uses semantic search with a high relevance_threshold. Returns True
    when the top hit's score exceeds _DEDUP_THRESHOLD — meaning we
    already have substantively the same memory.
    """
    try:
        async with async_session() as s:
            results = await search_memories(
                s, query=content, top_k=3,
                relevance_threshold=_DEDUP_THRESHOLD,
            )
        return len(results) > 0
    except Exception as e:
        # If dedup is unavailable, err on the side of NOT storing
        # (avoid creating dups). The user can always rerun later.
        logger.warning("[post-turn-extract] dedup check failed: %s", e)
        return True


def _normalize_candidate(c: object) -> dict | None:
    """Validate + coerce one candidate dict from the LLM. Returns None
    on anything malformed."""
    if not isinstance(c, dict):
        return None
    content = (c.get("content") or "").strip() if isinstance(c.get("content"), str) else ""
    if len(content) < 6:
        return None
    raw_type = (c.get("memory_type") or "").strip().lower()
    if raw_type == "semantic":
        memory_type = MemoryType.SEMANTIC
    else:
        # Default to EPISODIC for "episodic" or anything we don't recognize.
        memory_type = MemoryType.EPISODIC
    importance = c.get("importance")
    try:
        importance = float(importance) if importance is not None else 0.5
    except (ValueError, TypeError):
        importance = 0.5
    importance = max(0.0, min(1.0, importance))
    tags_raw = c.get("tags") or ""
    if isinstance(tags_raw, list):
        tags_raw = ",".join(str(t) for t in tags_raw)
    if not isinstance(tags_raw, str):
        tags_raw = ""
    tags_raw = tags_raw.strip(", ")
    # Always tag with auto_extracted so we can filter / audit memories
    # from this path separately from agent-driven and user-driven.
    if "auto_extracted" not in tags_raw:
        tags_raw = f"auto_extracted,{tags_raw}".rstrip(",")
    return {
        "content": content[:2000],
        "memory_type": memory_type,
        "importance": importance,
        "tags": tags_raw or "auto_extracted",
    }


async def extract_and_store(
    *,
    prompt: str,
    response: str,
    session_id: str | None = None,
) -> dict:
    """Extract candidate memories from one conversation turn, dedup,
    and store the survivors. Best-effort — never raises.

    Returns: {
      candidates: <int>,    # how many the LLM proposed
      stored: <int>,        # how many survived dedup + got persisted
      skipped_dup: <int>,
      skipped_invalid: <int>,
    }
    """
    if not prompt and not response:
        return {"candidates": 0, "stored": 0, "skipped_dup": 0, "skipped_invalid": 0}

    candidates_raw = await _llm_extract(prompt, response)
    skipped_invalid = 0
    skipped_dup = 0
    stored_count = 0

    candidates: list[dict] = []
    for raw in candidates_raw:
        norm = _normalize_candidate(raw)
        if norm is None:
            skipped_invalid += 1
            continue
        candidates.append(norm)

    for cand in candidates:
        try:
            if await _is_duplicate(cand["content"]):
                skipped_dup += 1
                continue
            async with async_session() as s:
                await store_memory(
                    s,
                    content=cand["content"],
                    memory_type=cand["memory_type"],
                    source="auto_extracted",
                    tags=cand["tags"],
                    importance=cand["importance"],
                )
            stored_count += 1
        except Exception as e:
            logger.warning(
                "[post-turn-extract] store failed for content=%r: %s",
                cand["content"][:80], e,
            )
            skipped_invalid += 1

    if stored_count > 0:
        logger.info(
            "[post-turn-extract] session=%s stored=%d (cand=%d dup=%d invalid=%d)",
            session_id or "?", stored_count, len(candidates_raw),
            skipped_dup, skipped_invalid,
        )

    return {
        "candidates": len(candidates_raw),
        "stored": stored_count,
        "skipped_dup": skipped_dup,
        "skipped_invalid": skipped_invalid,
    }
