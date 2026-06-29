"""
Voice-feedback loop — learn Kunal's voice from how he EDITS drafts.

The strongest voice signal there is: "Astra wrote X, Kunal changed it to Y
before sending." The send path already records `ai_original_body` (the AI
draft) vs the final `body_text` (what Kunal actually sent) on every edited
draft. This module distills those deltas into concise voice-correction
notes and stores them; the drafter appends them to its prompt, so the voice
COMPOUNDS toward how Kunal really writes instead of staying frozen at the
hand-written guide.

Storage is a `voice_profile` table self-created with CREATE TABLE IF NOT
EXISTS (email-agent has no migration auto-runner — same constraint that
pushed draft metrics into JSONB). The drafter reads it best-effort: no
table / no notes → it just uses the base voice, unchanged.
"""

from __future__ import annotations

import logging

import anthropic
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from email_agent.config import settings
from email_agent.models.draft import Draft, DraftStatus

logger = logging.getLogger(__name__)

_MIN_SAMPLES = 3   # below this there isn't enough signal to learn from
_MAX_SAMPLES = 15  # cap the prompt size / cost

_ENSURE = text(
    "CREATE TABLE IF NOT EXISTS voice_profile ("
    "channel text PRIMARY KEY, notes text NOT NULL DEFAULT '', "
    "sample_count int NOT NULL DEFAULT 0, "
    "updated_at timestamptz NOT NULL DEFAULT now())"
)

_DISTILL_SYSTEM = """You analyze how Kunal edits AI-drafted emails before sending them.

You're given pairs: the AI's ORIGINAL draft and Kunal's FINAL sent version.
The DELTA between them is how Kunal actually writes vs. how the AI guessed.

Distill ONLY the consistent, repeated patterns into a short list of
voice-correction rules the drafter should follow next time — e.g. "opens
with the ask, not a greeting", "cuts hedging like 'just wanted to'", "signs
'— K' not 'Best, Kunal'", "keeps it to 2-3 sentences", "drops exclamation
marks". Be specific and behavioral. Ignore one-off content changes (facts,
names, dates) — only capture STYLE/VOICE patterns that recur.

Output 4-8 terse bullet rules, one per line starting with "- ". No preamble.
If there's no consistent pattern, output a single line: "- (no consistent
edit pattern yet)"."""


async def learn_email_voice(session: AsyncSession) -> dict:
    """Distill Kunal's edit patterns into stored voice notes. Idempotent;
    safe to call on a schedule."""
    await session.execute(_ENSURE)
    await session.commit()

    rows = (
        (
            await session.execute(
                select(Draft)
                .where(Draft.status == DraftStatus.SENT)
                .order_by(Draft.updated_at.desc())
                .limit(80)
            )
        )
        .scalars()
        .all()
    )
    pairs: list[tuple[str, str]] = []
    for d in rows:
        ed = d.extra_data or {}
        original = ed.get("ai_original_body")
        if ed.get("was_edited") and original and d.body_text:
            pairs.append((original, d.body_text))
        if len(pairs) >= _MAX_SAMPLES:
            break

    if len(pairs) < _MIN_SAMPLES:
        logger.info("[voice_learn] only %d edited samples (<%d) — skipping",
                    len(pairs), _MIN_SAMPLES)
        return {"ok": True, "learned": False,
                "reason": f"only {len(pairs)} edited samples (need {_MIN_SAMPLES})"}

    blocks = []
    for i, (orig, final) in enumerate(pairs, 1):
        blocks.append(
            f"### Case {i}\nAI ORIGINAL:\n{orig[:1200]}\n\nKUNAL FINAL:\n{final[:1200]}"
        )
    user = (
        f"{len(pairs)} edit cases (AI draft → Kunal's sent version):\n\n"
        + "\n\n".join(blocks)
        + "\n\nDistill the recurring STYLE patterns into the bullet rules."
    )

    try:
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        resp = await client.messages.create(
            model=settings.model_sonnet,
            max_tokens=600,
            system=_DISTILL_SYSTEM,
            messages=[{"role": "user", "content": user}],
        )
        notes = "\n".join(b.text for b in resp.content if hasattr(b, "text")).strip()
    except Exception as e:
        logger.error("[voice_learn] distill failed: %s", e)
        return {"ok": False, "error": str(e)[:200]}

    await session.execute(
        text(
            "INSERT INTO voice_profile (channel, notes, sample_count, updated_at) "
            "VALUES ('email', :n, :c, now()) "
            "ON CONFLICT (channel) DO UPDATE SET "
            "notes = :n, sample_count = :c, updated_at = now()"
        ),
        {"n": notes, "c": len(pairs)},
    )
    await session.commit()
    logger.info("[voice_learn] learned email voice from %d edits", len(pairs))
    return {"ok": True, "learned": True, "samples": len(pairs), "notes": notes}


async def get_email_voice_notes(session: AsyncSession) -> str:
    """The learned voice notes for the drafter to append. Best-effort:
    missing table / no row → "" (drafter falls back to the base voice)."""
    try:
        r = await session.execute(
            text("SELECT notes FROM voice_profile WHERE channel = 'email'")
        )
        row = r.first()
        return (row[0] or "").strip() if row else ""
    except Exception:
        return ""
