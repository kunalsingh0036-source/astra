"""
Audit logger for Astra's actions.

Every action Astra takes is logged with:
- What: tool name and parameters
- When: timestamp
- Why: the autonomy mode and permission decision
- Outcome: allowed, denied, or asked

Each entry is kept in-memory for fast in-process reads AND persisted
to PostgreSQL (`audit_events` table) so the /audit page can render the
full cross-session trail.
"""

import asyncio
import logging
import threading
from datetime import datetime, timezone

from astra.autonomy.modes import ActionTier, AutonomyMode, PermissionDecision

logger = logging.getLogger(__name__)


class AuditEntry:
    """A single audit log entry."""

    __slots__ = (
        "timestamp",
        "tool_name",
        "action_tier",
        "autonomy_mode",
        "decision",
        "tool_input_summary",
        "context",
    )

    def __init__(
        self,
        tool_name: str,
        action_tier: ActionTier,
        autonomy_mode: AutonomyMode,
        decision: PermissionDecision,
        tool_input_summary: str = "",
        context: str = "",
    ):
        self.timestamp = datetime.now(timezone.utc)
        self.tool_name = tool_name
        self.action_tier = action_tier
        self.autonomy_mode = autonomy_mode
        self.decision = decision
        self.tool_input_summary = tool_input_summary
        self.context = context

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp.isoformat(),
            "tool_name": self.tool_name,
            "action_tier": self.action_tier.value,
            "autonomy_mode": self.autonomy_mode.value,
            "decision": self.decision.value,
            "tool_input_summary": self.tool_input_summary,
            "context": self.context,
        }


class AuditLogger:
    """Thread-safe audit logger for all Astra actions."""

    def __init__(self, max_entries: int = 10000):
        self._entries: list[AuditEntry] = []
        self._lock = threading.Lock()
        self._max_entries = max_entries

    def log(
        self,
        tool_name: str,
        action_tier: ActionTier,
        autonomy_mode: AutonomyMode,
        decision: PermissionDecision,
        tool_input_summary: str = "",
        context: str = "",
    ) -> AuditEntry:
        """Log an action."""
        entry = AuditEntry(
            tool_name=tool_name,
            action_tier=action_tier,
            autonomy_mode=autonomy_mode,
            decision=decision,
            tool_input_summary=tool_input_summary,
            context=context,
        )
        with self._lock:
            self._entries.append(entry)
            # Trim if over limit
            if len(self._entries) > self._max_entries:
                self._entries = self._entries[-self._max_entries:]

        # Persist to DB in the background so the hook returns fast.
        # If there's no running event loop (e.g. during unit tests)
        # we skip persistence rather than blocking.
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_persist_entry(entry))
        except RuntimeError:
            pass  # no loop; memory-only fallback
        return entry

    def get_entries(
        self,
        limit: int = 50,
        tool_name: str | None = None,
        decision: PermissionDecision | None = None,
    ) -> list[dict]:
        """Get recent audit entries with optional filtering."""
        with self._lock:
            entries = self._entries[:]

        if tool_name:
            entries = [e for e in entries if e.tool_name == tool_name]
        if decision:
            entries = [e for e in entries if e.decision == decision]

        return [e.to_dict() for e in entries[-limit:]]

    def get_stats(self) -> dict:
        """Get audit statistics."""
        with self._lock:
            entries = self._entries[:]

        total = len(entries)
        if total == 0:
            return {"total": 0, "by_decision": {}, "by_tier": {}, "by_tool": {}}

        by_decision = {}
        by_tier = {}
        by_tool = {}

        for e in entries:
            d = e.decision.value
            by_decision[d] = by_decision.get(d, 0) + 1
            t = e.action_tier.value
            by_tier[t] = by_tier.get(t, 0) + 1
            by_tool[e.tool_name] = by_tool.get(e.tool_name, 0) + 1

        return {
            "total": total,
            "by_decision": by_decision,
            "by_tier": by_tier,
            "by_tool": dict(sorted(by_tool.items(), key=lambda x: -x[1])[:10]),
        }


async def _persist_entry(entry: AuditEntry) -> None:
    """Write one audit entry to the DB. Swallows errors — audit
    logging must never break the agent loop."""
    try:
        from astra.autonomy.models import AuditEvent
        from astra.db.engine import async_session

        async with async_session() as session:
            session.add(
                AuditEvent(
                    ts=entry.timestamp,
                    tool_name=entry.tool_name,
                    action_tier=entry.action_tier.value,
                    autonomy_mode=entry.autonomy_mode.value,
                    decision=entry.decision.value,
                    tool_input_summary=entry.tool_input_summary or "",
                    context=entry.context or "",
                )
            )
            await session.commit()
    except Exception:
        logger.exception("failed to persist audit entry")


# Global singleton
audit_logger = AuditLogger()
