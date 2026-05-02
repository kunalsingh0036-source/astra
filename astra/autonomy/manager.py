"""
Autonomy mode manager.

Handles mode transitions with three toggle mechanisms:
1. Manual: User explicitly sets the mode
2. Time-based: Mode reverts after a duration ("semi-auto for 2 hours")
3. Task-based: Mode applies for a specific task scope, then reverts

Thread-safe via a simple lock (single-user system, but future-proofing).
"""

import threading
import time
from datetime import datetime, timezone

from astra.autonomy.modes import AutonomyMode
from astra.config import settings


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


# Global singleton
autonomy_manager = AutonomyManager()
