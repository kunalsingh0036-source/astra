"""
Session message persistence for the lean runtime.

The legacy SDK runtime kept conversation state in the bundled CLI's
subprocess memory — which evaporated on every container restart. The
lean runtime stores the full message history in Postgres so a session
genuinely survives deploys, restarts, and refreshes.

Schema: extending the existing `turns` table (migration n2g58h4f9c1c)
with a `messages` JSONB column (migration o3h69i5g0d2d). Each turn's
final message stack is written on completion.

API:
  load_session_messages(session_id) → list[dict]
    Returns the chronological message stack for a session by stitching
    the messages stored on each turn in started_at order.
  save_turn_messages(turn_id, messages) → None
    Writes the full message stack on the turn row.

Tolerates missing column (column doesn't exist yet pre-migration) by
falling back to empty history. Tolerates DB unavailability by raising
— the caller decides whether to proceed without history.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import text

from astra.db.engine import async_session

logger = logging.getLogger(__name__)


async def load_session_messages(session_id: str) -> list[dict[str, Any]]:
    """Stitch the message history of a session from prior completed turns.

    Each row in `turns` carries that turn's final messages JSONB. We
    concatenate in started_at order. Skips rows where messages is null
    or empty (interrupted/failed turns whose messages were never saved).

    Returns an empty list when the session is new or the table doesn't
    yet have the messages column (migration not run on this DB).
    """
    if not session_id:
        return []
    try:
        async with async_session() as s:
            r = await s.execute(
                text(
                    """
                    SELECT messages
                    FROM turns
                    WHERE session_id = :sid
                      AND status = 'complete'
                      AND messages IS NOT NULL
                    ORDER BY started_at ASC
                    """
                ),
                {"sid": session_id},
            )
            rows = r.all()
    except Exception:
        logger.exception("[session-store] load failed for session=%s", session_id)
        return []

    out: list[dict[str, Any]] = []
    for row in rows:
        msgs = row[0]
        if not msgs:
            continue
        # JSONB returns a Python list/dict already in asyncpg
        if isinstance(msgs, str):
            try:
                msgs = json.loads(msgs)
            except json.JSONDecodeError:
                continue
        if isinstance(msgs, list):
            out.extend(m for m in msgs if isinstance(m, dict))
    return out


def _strip_image_blocks(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Replace base64 image content blocks with a small text marker.

    Why HERE, at the persistence boundary: a drag-dropped screenshot
    arrives as a multi-MB base64 image block on the user message. If
    that block is persisted into turns.messages, three things go
    wrong on every later turn in the session:
      1. The base64 re-ships to the Anthropic API each turn until it
         falls out of the compaction tail (multi-MB of upload per
         message sent).
      2. The char//4 token estimator counts base64 at ~200× the real
         image token cost (1MB PNG ≈ 343k estimated vs ~1.6k real),
         so the session crosses the compaction threshold permanently
         and pass-2 elides ALL history — user-visible as "Astra
         forgot the conversation after I sent a screenshot."
      3. turns.messages grows without bound (no retention job yet).

    Semantics: the model SAW the image during the turn it was sent —
    its text response captures what mattered. History keeps a marker
    so the model knows an image existed and can ask the user to
    re-attach if it genuinely needs another look.
    """
    out: list[dict[str, Any]] = []
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            out.append(msg)
            continue
        new_content: list[Any] = []
        changed = False
        for block in content:
            if isinstance(block, dict) and block.get("type") == "image":
                changed = True
                new_content.append(
                    {
                        "type": "text",
                        "text": (
                            "[image attachment from this turn — analyzed "
                            "live, not re-sent in later context. Ask the "
                            "user to re-attach if you need to see it again.]"
                        ),
                    }
                )
            else:
                new_content.append(block)
        if changed:
            out.append({**msg, "content": new_content})
        else:
            out.append(msg)
    return out


async def save_turn_messages(
    turn_id: int, messages: list[dict[str, Any]]
) -> None:
    """Write the full message stack onto a turn row.

    Called at the end of run_lean_turn so the next turn in the session
    can rehydrate. Image content blocks are stripped to text markers
    first (see _strip_image_blocks). Swallows errors — persistence
    failures must never break the user's actual turn.
    """
    if turn_id is None:
        return
    try:
        messages = _strip_image_blocks(messages)
        async with async_session() as s:
            await s.execute(
                text(
                    """
                    UPDATE turns
                    SET messages = CAST(:m AS JSONB)
                    WHERE id = :id
                    """
                ),
                {"id": int(turn_id), "m": json.dumps(messages)},
            )
            await s.commit()
    except Exception:
        logger.exception("[session-store] save failed for turn=%s", turn_id)
