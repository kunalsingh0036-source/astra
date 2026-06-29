"""
Cloud source of truth for the 6 missed-session debt counters.

The counters (stretch/meditate/breathe/movement/skill/workout) used to live
only in the "Kunal" Apple Note, reaching the cloud solely when the Mac was
awake + the bridge synced. That's the macOS fault-line: training context
went STALE whenever the laptop was off. This module moves the source of
truth to a cloud `training_counters` row that Kunal updates over WhatsApp,
so the daily snapshot + trend + "Kunal Now" stay live regardless of the Mac.

Design notes:
- The Apple Note is now a BOOTSTRAP/FALLBACK: on the first cloud write we
  seed from the note's last-known values; `current_counters()` (in
  missed_sessions) reads cloud-first and only parses the note if the cloud
  row doesn't exist yet.
- Every read/write runs on its OWN isolated session and ensures the table at
  point of use (process-guarded). A missing table can therefore never poison
  a caller's transaction nor crash — the lesson from the voice_profile bug.
"""

from __future__ import annotations

import logging

from sqlalchemy import text

from astra.db.engine import async_session
from astra.notes.missed_sessions import TYPES, Counters

logger = logging.getLogger(__name__)

_ensured = False  # process-level guard so we CREATE TABLE at most once/process

_ENSURE = text(
    "CREATE TABLE IF NOT EXISTS training_counters ("
    "id INTEGER PRIMARY KEY DEFAULT 1, "
    "stretch INTEGER, meditate INTEGER, breathe INTEGER, "
    "movement INTEGER, skill INTEGER, workout INTEGER, "
    "updated_via TEXT NOT NULL DEFAULT '', "
    "last_note TEXT NOT NULL DEFAULT '', "
    "updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), "
    "CONSTRAINT training_counters_singleton CHECK (id = 1))"
)


async def ensure_training_table() -> None:
    """Idempotent CREATE TABLE on an isolated session. The alembic migration
    handles fresh DBs + history; this guards the deploy window before the
    scheduler's `alembic upgrade head` has run."""
    global _ensured
    if _ensured:
        return
    try:
        async with async_session() as s:
            await s.execute(_ENSURE)
            await s.commit()
        _ensured = True
    except Exception as e:
        logger.warning("[training_state] ensure failed: %s", e)


def _row_to_counters(row) -> Counters | None:
    if row is None:
        return None
    vals = {t: row._mapping.get(t) for t in TYPES}
    if all(v is None for v in vals.values()):
        return None
    return Counters(**vals)


async def get_cloud_counters() -> Counters | None:
    """The current cloud counters, or None if not seeded yet. Isolated +
    best-effort: any failure → None (caller falls back to the Apple Note)."""
    await ensure_training_table()
    try:
        async with async_session() as s:
            r = await s.execute(
                text(
                    "SELECT stretch, meditate, breathe, movement, skill, workout "
                    "FROM training_counters WHERE id = 1"
                )
            )
            return _row_to_counters(r.first())
    except Exception as e:
        logger.info("[training_state] get_cloud_counters: %s", e)
        return None


async def cloud_meta() -> dict:
    """updated_at / updated_via for the status tool. Best-effort."""
    await ensure_training_table()
    try:
        async with async_session() as s:
            r = await s.execute(
                text("SELECT updated_via, updated_at FROM training_counters WHERE id = 1")
            )
            row = r.first()
            if not row:
                return {}
            return {
                "updated_via": row._mapping.get("updated_via"),
                "updated_at": row._mapping.get("updated_at"),
            }
    except Exception:
        return {}


async def apply_update(
    *,
    set_map: dict[str, int] | None = None,
    delta_map: dict[str, int] | None = None,
    via: str = "chat",
    note: str = "",
) -> tuple[Counters, list[str]]:
    """Apply absolute sets and/or relative deltas to the cloud counters and
    return (new_state, skipped_unknown).

    Hard rule (mirrors writeback.py's "a partial parse would corrupt the
    ledger"): an UNKNOWN counter is NULL, never 0. We never overwrite a known
    cumulative debt with a fabricated zero, and a relative delta against an
    unknown baseline is REFUSED (returned in skipped_unknown) rather than
    silently started from 0. Absolute sets and known-base deltas floor at 0.
    """
    set_map = set_map or {}
    delta_map = delta_map or {}
    await ensure_training_table()

    # Base = current cloud state, or the note's last-known values on first
    # write (bootstrap). Either may be PARTIAL — missing types stay None.
    base = await get_cloud_counters()
    if base is None:
        from astra.notes.missed_sessions import current_counters

        base = await current_counters()  # note fallback; may be None/partial
    vals: dict[str, int | None] = {}
    for t in TYPES:
        cur = getattr(base, t, None) if base else None
        vals[t] = int(cur) if cur is not None else None  # None = unknown

    for t, v in set_map.items():
        if t in vals and v is not None:
            vals[t] = max(0, int(v))

    skipped_unknown: list[str] = []
    for t, d in delta_map.items():
        if t not in vals or d is None:
            continue
        if vals[t] is None:
            # no baseline → can't apply a relative change; surface it
            skipped_unknown.append(t)
            continue
        vals[t] = max(0, vals[t] + int(d))

    async with async_session() as s:
        await s.execute(_ENSURE)
        # COALESCE so a NULL (unknown) parameter never clobbers an existing
        # non-NULL column on the update path.
        await s.execute(
            text(
                "INSERT INTO training_counters "
                "(id, stretch, meditate, breathe, movement, skill, workout, "
                " updated_via, last_note, updated_at) "
                "VALUES (1, :stretch, :meditate, :breathe, :movement, :skill, "
                " :workout, :via, :note, now()) "
                "ON CONFLICT (id) DO UPDATE SET "
                "stretch=COALESCE(:stretch, training_counters.stretch), "
                "meditate=COALESCE(:meditate, training_counters.meditate), "
                "breathe=COALESCE(:breathe, training_counters.breathe), "
                "movement=COALESCE(:movement, training_counters.movement), "
                "skill=COALESCE(:skill, training_counters.skill), "
                "workout=COALESCE(:workout, training_counters.workout), "
                "updated_via=:via, last_note=:note, updated_at=now()"
            ),
            {**vals, "via": via[:40], "note": (note or "")[:500]},
        )
        await s.commit()

    logger.info("[training_state] updated via %s: %s (skipped=%s)",
                via, vals, skipped_unknown)
    # Re-read so the echo reflects the true stored row (COALESCE may have kept
    # prior values where we passed NULL).
    new = await get_cloud_counters()
    return (new or Counters(**{t: vals[t] for t in TYPES})), skipped_unknown
