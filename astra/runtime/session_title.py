"""
Session title generation.

After a session's FIRST turn completes, we generate a 4-8 word topic
title via Anthropic Haiku and store it in `session_titles`. The /sessions
list shows this title prominently, with the (truncated) first prompt
as a subtitle.

Why Haiku, not Sonnet:
  - The task is short, low-stakes, embarrassingly cheap.
  - claude-3-5-haiku at $0.80/M input + $4/M output ≈ $0.0001 per
    title. Sonnet would be ~12× pricier for no quality gain on a
    one-line summarization task.

Why background-async (not synchronous):
  - First-turn completion already returns the agent's response.
    Adding a 500-1000ms title call to the path would slow the
    perceived first response.
  - The title is metadata; missing it briefly is harmless. The /api/
    sessions endpoint LEFT JOINs and falls back to truncated first
    prompt when no title row exists yet.

API:
    generate_and_store_title(session_id) -> str | None
        Idempotent. If a title already exists for the session, returns
        the existing one without regenerating.

    fire_and_forget(session_id) -> None
        Schedule generation as an asyncio task. Used from the agent
        loop's finalize block; returns immediately, never raises.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from anthropic import AsyncAnthropic
from sqlalchemy import text

from astra.db.engine import async_session

logger = logging.getLogger(__name__)

# Haiku is the cheapest current Claude model. If a future cheaper
# model lands, swap here without touching call sites.
TITLE_MODEL = os.environ.get("ASTRA_TITLE_MODEL", "claude-haiku-4-5")
TITLE_MAX_TOKENS = 80  # plenty for 4-8 words; bounded so misbehavior is cheap
TITLE_PROMPT_BUDGET = 1500  # truncate first prompt + first response to this many chars

_TITLE_SYSTEM = (
    "You generate short topic titles for chat sessions. "
    "Read the user's first prompt and the assistant's first response. "
    "Output ONE line: 4 to 8 words describing what the conversation is about. "
    "No quotes, no periods, no leading/trailing whitespace. "
    "Use lowercase except proper nouns and acronyms.\n"
    "\n"
    "ABSOLUTE RULES:\n"
    "  1. ALWAYS output a title. Never refuse. Never explain.\n"
    "  2. NEVER write meta-commentary like 'I don't have enough context' or 'I need more information' — just generate the best title possible from whatever's there.\n"
    "  3. If the prompt is vague (a single yes/ok/'try it now'), title it generically: 'short confirmation', 'follow-up reply', 'continuation request', 'quick check-in'.\n"
    "  4. NEVER exceed 8 words.\n"
    "  5. NEVER include the word 'title' in the output.\n"
    "\n"
    "Good examples:\n"
    "  '375 Studio website analysis'\n"
    "  'film noir color palette'\n"
    "  'apex sales agent debugging'\n"
    "  \"tonight's dinner ideas\"\n"
    "  'short confirmation reply'\n"
    "  'continuation of earlier task'\n"
    "Bad examples (do NOT do these):\n"
    "  'The user asked about colors' — meta, not topical\n"
    "  'Color palette selection for a film noir aesthetic style' — too long\n"
    "  'Studio 375.' — has period\n"
    "  'I need more context to generate a title' — REFUSAL, never do this\n"
    "  'I don't have enough information' — REFUSAL, never do this"
)


def _title_looks_bad(title: str) -> bool:
    """Reject titles that are model refusals or otherwise unusable.
    Caller should fall back to the truncated first prompt when this
    returns True."""
    if not title:
        return True
    # Length cap — well above 8 short words. If it's longer, the model
    # almost certainly wrote prose instead of a title.
    if len(title) > 80:
        return True
    low = title.lower()
    refusal_markers = (
        "i don't have",
        "i don't ",
        "i do not have",
        "i need more",
        "i cannot",
        "i can't",
        "without knowing",
        "more context",
        "not enough context",
        "without additional",
        "based on the",
        "the user asked",
        "the user wants",
        "this conversation",
        "as an ai",
    )
    if any(m in low for m in refusal_markers):
        return True
    # If Haiku ignored "no quotes/periods" rules so badly that the
    # output is mostly punctuation, drop it.
    alpha_ratio = sum(c.isalpha() or c.isspace() for c in title) / max(
        len(title), 1
    )
    if alpha_ratio < 0.5:
        return True
    return False


async def _existing_title(session_id: str) -> str | None:
    """Return the stored title for a session, or None."""
    async with async_session() as s:
        r = await s.execute(
            text("SELECT title FROM session_titles WHERE session_id = :sid"),
            {"sid": session_id},
        )
        row = r.first()
    return row.title if row else None


async def _first_turn_for_session(session_id: str) -> dict[str, Any] | None:
    """Pull the prompt + response of the session's first turn.

    Originally filtered to status='complete' only, but that excluded
    sessions whose only turn was interrupted/failed — leaving them
    untitled in the UI ("(none)"). Worse UX than a title generated
    from prompt-only.

    Now accepts any status. Prefers turns with a non-empty response
    (more context for Haiku → better title) but falls back to
    prompt-only if necessary. The Haiku call handles the empty-
    response case gracefully — the prompt alone is usually enough.
    """
    async with async_session() as s:
        r = await s.execute(
            text(
                """
                SELECT prompt, response, status
                FROM turns
                WHERE session_id = :sid
                  AND prompt IS NOT NULL
                  AND prompt <> ''
                ORDER BY
                  -- prefer turns with a non-empty response (more
                  -- signal for the title), then by chronology
                  CASE WHEN response IS NOT NULL AND response <> ''
                       THEN 0 ELSE 1 END,
                  started_at ASC
                LIMIT 1
                """
            ),
            {"sid": session_id},
        )
        row = r.first()
    if not row:
        return None
    return {"prompt": row.prompt or "", "response": row.response or ""}


def _normalize_title(raw: str) -> str:
    """Strip quotes, periods, surrounding whitespace; cap length."""
    title = (raw or "").strip()
    # Strip wrapping quotes if model added them
    if (title.startswith('"') and title.endswith('"')) or (
        title.startswith("'") and title.endswith("'")
    ):
        title = title[1:-1].strip()
    # Drop trailing period
    while title.endswith("."):
        title = title[:-1].rstrip()
    # Hard cap so a runaway response doesn't blow out the column
    if len(title) > 120:
        title = title[:117].rstrip() + "…"
    return title


async def _store_title(session_id: str, title: str, source: str) -> None:
    """Insert a title row. Uses ON CONFLICT to be idempotent — if a
    title already exists, it's preserved (caller has already checked
    via _existing_title, but the constraint protects against races)."""
    async with async_session() as s:
        await s.execute(
            text(
                """
                INSERT INTO session_titles (session_id, title, source)
                VALUES (:sid, :t, :src)
                ON CONFLICT (session_id) DO NOTHING
                """
            ),
            {"sid": session_id, "t": title, "src": source},
        )
        await s.commit()


async def generate_and_store_title(session_id: str) -> str | None:
    """Generate a topic title and persist it. Idempotent; returns
    the stored title (existing or freshly generated). Returns None
    if the session has no completed turns yet (caller should retry
    later) or if generation fails."""
    if not session_id:
        return None

    # Idempotence: if already titled, return it.
    existing = await _existing_title(session_id)
    if existing:
        return existing

    first = await _first_turn_for_session(session_id)
    if not first:
        return None

    prompt = (first["prompt"] or "")[:TITLE_PROMPT_BUDGET]
    response = (first["response"] or "")[:TITLE_PROMPT_BUDGET]
    if not prompt:
        return None

    user_msg = (
        f"USER PROMPT:\n{prompt}\n\n"
        f"ASSISTANT RESPONSE:\n{response}\n\n"
        f"Title:"
    )

    try:
        client = AsyncAnthropic()
        msg = await client.messages.create(
            model=TITLE_MODEL,
            max_tokens=TITLE_MAX_TOKENS,
            system=_TITLE_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception as e:
        logger.warning(
            "[session-title] generation failed for session=%s: %s",
            session_id,
            e,
        )
        # Fall back to a deterministic snippet of the first prompt.
        # Better than no title; the source field flags it so a future
        # job could retry the Haiku call.
        fallback = _normalize_title(prompt[:80].split("\n")[0])
        if fallback:
            await _store_title(session_id, fallback, "fallback")
            return fallback
        return None

    raw = ""
    for block in msg.content:
        if getattr(block, "type", None) == "text":
            raw += getattr(block, "text", "")
    title = _normalize_title(raw)
    # Validate: Haiku sometimes refuses for thin-context prompts and
    # writes "I don't have enough context to generate a title…"
    # That's worse than a truncated first prompt — at least the
    # prompt is real signal. Reject + fall back.
    if not title or _title_looks_bad(title):
        if title:
            logger.info(
                "[session-title] rejected bad output for session=%s: %r",
                session_id,
                title[:80],
            )
        fallback = _normalize_title(prompt[:60].split("\n")[0])
        if fallback:
            await _store_title(session_id, fallback, "fallback")
            return fallback
        return None
    await _store_title(session_id, title, "haiku")
    logger.info(
        "[session-title] generated for session=%s: %r",
        session_id,
        title,
    )
    return title


def fire_and_forget(session_id: str) -> None:
    """Schedule title generation. Returns immediately. Never raises.

    Called from the agent loop's finalize block. Generation runs after
    the user's response has already been returned, so any latency here
    is invisible. Errors are logged but not propagated — a missing
    title is far less bad than a failed turn.
    """
    if not session_id:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No event loop — the caller is sync. Skip; the next turn or
        # backfill will catch this session.
        return
    loop.create_task(_safe_generate(session_id))


async def _safe_generate(session_id: str) -> None:
    """Wrapper that swallows all exceptions so a misbehaving Haiku
    call (rate limit, model deprecation, etc.) can't crash the
    runtime."""
    try:
        await generate_and_store_title(session_id)
    except Exception:
        logger.exception(
            "[session-title] _safe_generate failed for session=%s",
            session_id,
        )
