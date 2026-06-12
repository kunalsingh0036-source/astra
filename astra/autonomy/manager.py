"""
Autonomy mode manager.

Handles mode transitions with three toggle mechanisms:
1. Manual: User explicitly sets the mode
2. Time-based: Mode reverts after a duration ("semi-auto for 2 hours")
3. Task-based: Mode applies for a specific task scope, then reverts

Cross-service persistence: the web service (astra-web) and the
stream service (astra/services/stream) run in separate Railway
containers with their own process memory. The web UI's autonomy
toggle writes to the `app_settings` table; this manager reads from
that table at the start of every turn so a UI switch propagates to
the agent runtime within ~1 turn. Without this, the stream
service's in-memory _mode silently diverges from what the user
sees in /settings — exactly the bug Kunal reported when "semi_auto"
in the UI didn't stop the agent from saying "I need your approval."

Thread-safe via a simple lock (single-user system, but future-proofing).
"""

import logging
import threading
import time
from datetime import datetime, timezone

from astra.autonomy.modes import AutonomyMode
from astra.config import settings

logger = logging.getLogger(__name__)

# Key inside the app_settings table that holds the autonomy mode.
# Matches /api/autonomy in astra-web — they read/write the same row.
_DB_KEY = "autonomy_mode"


async def _read_mode_from_db() -> str | None:
    """Read the autonomy_mode value from app_settings.

    Returns None if the row is missing, the DB is unreachable, or
    the value isn't a recognised mode. Callers fall back to the
    in-memory mode in any of these cases — we never crash a turn
    over a config-table problem.
    """
    try:
        from sqlalchemy import text

        from astra.db.engine import async_session
    except Exception as e:
        logger.warning("[autonomy] DB engine unavailable: %s", e)
        return None
    try:
        async with async_session() as s:
            r = await s.execute(
                text("SELECT value FROM app_settings WHERE key = :k"),
                {"k": _DB_KEY},
            )
            row = r.first()
        if not row:
            return None
        value = str(row.value or "").strip()
        if value not in {m.value for m in AutonomyMode}:
            logger.warning("[autonomy] unknown mode in DB: %r", value)
            return None
        return value
    except Exception as e:
        logger.warning("[autonomy] DB read failed: %s", e)
        return None


async def _write_mode_to_db(mode: AutonomyMode) -> bool:
    """Upsert the autonomy_mode row. Best effort — returns False on
    failure but never raises (config-table writes can't fail a turn)."""
    try:
        from sqlalchemy import text

        from astra.db.engine import async_session
    except Exception as e:
        logger.warning("[autonomy] DB engine unavailable for write: %s", e)
        return False
    try:
        async with async_session() as s:
            await s.execute(
                text(
                    """
                    INSERT INTO app_settings (key, value, updated_at)
                    VALUES (:k, :v, now())
                    ON CONFLICT (key)
                    DO UPDATE SET value = EXCLUDED.value, updated_at = now()
                    """
                ),
                {"k": _DB_KEY, "v": mode.value},
            )
            await s.commit()
        return True
    except Exception as e:
        logger.warning("[autonomy] DB write failed: %s", e)
        return False


class AutonomyManager:
    """Manages the current autonomy mode and transitions."""

    def __init__(self):
        self._mode = AutonomyMode(settings.default_autonomy_mode)
        self._previous_mode: AutonomyMode | None = None
        self._revert_at: float | None = None  # Unix timestamp for time-based revert
        self._task_scope: str | None = None  # Task ID for task-based mode
        self._lock = threading.Lock()
        self._history: list[dict] = []  # Mode transition history

    @property
    def mode(self) -> AutonomyMode:
        """Get the current autonomy mode, checking for time-based revert."""
        with self._lock:
            self._check_revert()
            return self._mode

    def set_mode(
        self,
        mode: AutonomyMode,
        duration_minutes: int | None = None,
        task_id: str | None = None,
        reason: str = "",
    ) -> dict:
        """Set the autonomy mode.

        Args:
            mode: The new mode.
            duration_minutes: If set, revert to previous mode after this many minutes.
            task_id: If set, revert when this task completes.
            reason: Why the mode is being changed (for audit log).

        Returns:
            Dict with mode change details.
        """
        with self._lock:
            old_mode = self._mode
            self._previous_mode = old_mode
            self._mode = mode

            if duration_minutes:
                self._revert_at = time.time() + (duration_minutes * 60)
            else:
                self._revert_at = None

            self._task_scope = task_id

            transition = {
                "from": old_mode.value,
                "to": mode.value,
                "reason": reason,
                "duration_minutes": duration_minutes,
                "task_id": task_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            self._history.append(transition)
            _persist_transition_async(
                old_mode.value, mode.value, reason, "set_mode"
            )

            return transition

    def complete_task(self, task_id: str) -> bool:
        """Signal that a task is complete. Reverts mode if it was task-scoped.

        Returns True if mode was reverted.
        """
        with self._lock:
            if self._task_scope == task_id and self._previous_mode:
                old = self._mode
                self._mode = self._previous_mode
                self._previous_mode = None
                self._task_scope = None
                self._history.append(
                    {
                        "from": old.value,
                        "to": self._mode.value,
                        "reason": f"Task {task_id} completed, reverting",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                )
                _persist_transition_async(
                    old.value,
                    self._mode.value,
                    f"task {task_id} completed",
                    "task_revert",
                )
                _persist_mode_value_async(self._mode)
                return True
            return False

    def _check_revert(self):
        """Check if a time-based mode should revert. Must hold lock."""
        if self._revert_at and time.time() >= self._revert_at:
            old = self._mode
            self._mode = self._previous_mode or AutonomyMode.ALWAYS_ASK
            self._previous_mode = None
            self._revert_at = None
            self._history.append(
                {
                    "from": old.value,
                    "to": self._mode.value,
                    "reason": "Time-based mode expired, reverting",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            )
            _persist_transition_async(
                old.value, self._mode.value, "time-based revert", "time_revert"
            )
            _persist_mode_value_async(self._mode)

    def get_status(self) -> dict:
        """Get current autonomy status."""
        with self._lock:
            self._check_revert()
            remaining = None
            if self._revert_at:
                remaining = max(0, int((self._revert_at - time.time()) / 60))

            return {
                "current_mode": self._mode.value,
                "previous_mode": self._previous_mode.value if self._previous_mode else None,
                "time_remaining_minutes": remaining,
                "task_scope": self._task_scope,
            }

    def get_history(self, limit: int = 20) -> list[dict]:
        """Get recent mode transition history."""
        return self._history[-limit:]

    # ── DB-backed cross-service persistence ────────────────────

    async def refresh_from_db(self) -> bool:
        """Pull the latest mode from app_settings.

        Call at the start of every turn so a /settings UI toggle in
        astra-web propagates here before the autonomy gate runs.
        Returns True if the local mode was updated, False otherwise
        (DB unreachable, value unchanged, or value invalid).
        """
        value = await _read_mode_from_db()
        if value is None:
            return False
        try:
            new_mode = AutonomyMode(value)
        except ValueError:
            return False
        with self._lock:
            if new_mode == self._mode:
                return False
            old = self._mode
            self._mode = new_mode
            self._history.append(
                {
                    "from": old.value,
                    "to": new_mode.value,
                    "reason": "Refreshed from app_settings (cross-service sync)",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            )
            _persist_transition_async(
                old.value, new_mode.value, "cross-service sync", "refresh"
            )
            logger.info(
                "[autonomy] mode synced from DB: %s -> %s",
                old.value,
                new_mode.value,
            )
            return True

    async def set_mode_persisted(
        self,
        mode: AutonomyMode,
        duration_minutes: int | None = None,
        task_id: str | None = None,
        reason: str = "",
    ) -> dict:
        """Like set_mode(), but also writes through to app_settings
        so the web UI / other services see the change.

        Use this from agent tools (set_mode_tool) and any code path
        where a mode change should be cross-service visible. The
        plain set_mode() is kept for tests and time/task-revert
        bookkeeping where DB writes aren't desired.
        """
        result = self.set_mode(
            mode=mode,
            duration_minutes=duration_minutes,
            task_id=task_id,
            reason=reason,
        )
        await _write_mode_to_db(mode)
        return result


def _persist_transition_async(
    from_mode: str, to_mode: str, reason: str, source: str
) -> None:
    """Fire-and-forget insert into autonomy_mode_history. Sync-callable
    (the manager's lock-holding methods are sync); schedules onto the
    running loop when there is one, silently skips otherwise — the
    in-memory _history still has the entry, and the next loop-borne
    transition persists normally. Never raises."""
    async def _write() -> None:
        try:
            from sqlalchemy import text as _sql

            from astra.db.engine import async_session

            async with async_session() as s:
                await s.execute(
                    _sql(
                        """
                        INSERT INTO autonomy_mode_history
                            (from_mode, to_mode, reason, source)
                        VALUES (:f, :t, :r, :s)
                        """
                    ),
                    {"f": from_mode, "t": to_mode, "r": reason[:1000], "s": source},
                )
                await s.commit()
        except Exception as e:
            logger.warning("[autonomy] history persist failed: %s", e)

    try:
        import asyncio

        asyncio.get_running_loop().create_task(_write())
    except RuntimeError:
        pass  # no loop (sync caller at import time) — skip


def _persist_mode_value_async(mode: AutonomyMode) -> None:
    """Fire-and-forget write-through of the CURRENT mode value to
    app_settings. Fixes the time/task-revert split-brain: the local
    revert used to be silently undone next turn when refresh_from_db
    re-adopted the still-persisted temporary mode."""
    async def _write() -> None:
        await _write_mode_to_db(mode)

    try:
        import asyncio

        asyncio.get_running_loop().create_task(_write())
    except RuntimeError:
        pass


# Global singleton
autonomy_manager = AutonomyManager()
