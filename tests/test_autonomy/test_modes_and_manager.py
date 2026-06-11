"""Tests for the autonomy mode system."""

import time
from unittest.mock import patch

import pytest

from astra.autonomy.audit import AuditLogger
from astra.autonomy.manager import AutonomyManager
from astra.autonomy.modes import (
    ActionTier,
    AutonomyMode,
    PermissionDecision,
    get_action_tier,
    get_permission,
)
from astra.config import settings


class TestModes:
    def test_always_ask_asks_for_everything(self):
        assert get_permission(AutonomyMode.ALWAYS_ASK, "Read") == PermissionDecision.ASK
        assert get_permission(AutonomyMode.ALWAYS_ASK, "Bash") == PermissionDecision.ASK
        assert get_permission(AutonomyMode.ALWAYS_ASK, "Edit") == PermissionDecision.ASK

    def test_semi_auto_allows_reads_asks_destructive(self):
        assert get_permission(AutonomyMode.SEMI_AUTO, "Read") == PermissionDecision.ALLOW
        assert get_permission(AutonomyMode.SEMI_AUTO, "Edit") == PermissionDecision.ALLOW
        assert get_permission(AutonomyMode.SEMI_AUTO, "Bash") == PermissionDecision.ASK

    def test_full_auto_allows_everything(self):
        assert get_permission(AutonomyMode.FULL_AUTO, "Read") == PermissionDecision.ALLOW
        assert get_permission(AutonomyMode.FULL_AUTO, "Bash") == PermissionDecision.ALLOW
        assert get_permission(AutonomyMode.FULL_AUTO, "forget_memory") == PermissionDecision.ALLOW

    def test_unknown_tool_defaults_to_write(self):
        assert get_action_tier("some_unknown_tool") == ActionTier.WRITE

    def test_tool_tier_classification(self):
        assert get_action_tier("Read") == ActionTier.READ
        assert get_action_tier("Edit") == ActionTier.WRITE
        assert get_action_tier("Bash") == ActionTier.DESTRUCTIVE

    def test_tier_based_permission_ignores_name_map(self):
        """The lean runtime gates on the REGISTERED tier, not the
        legacy name map. Regression lock for the local_bash bypass:
        the name map only knew SDK-era "Bash", so "local_bash" fell
        to the WRITE default and auto-ran arbitrary shell in
        semi_auto. With the tier-based path, a DESTRUCTIVE
        registration must yield ASK in semi_auto regardless of what
        the tool is called."""
        from astra.autonomy.modes import get_permission_for_tier

        # local_bash is NOT in TOOL_TIERS — the name path is wrong:
        assert get_action_tier("local_bash") == ActionTier.WRITE
        # …but the tier path (what the runtime now uses) is right:
        assert (
            get_permission_for_tier(AutonomyMode.SEMI_AUTO, ActionTier.DESTRUCTIVE)
            == PermissionDecision.ASK
        )
        assert (
            get_permission_for_tier(AutonomyMode.ALWAYS_ASK, ActionTier.DESTRUCTIVE)
            == PermissionDecision.ASK
        )
        assert (
            get_permission_for_tier(AutonomyMode.FULL_AUTO, ActionTier.DESTRUCTIVE)
            == PermissionDecision.ALLOW
        )

    def test_runtime_gate_denies_destructive_tier_in_semi_auto(self):
        """End-to-end through _autonomy_check: a ToolDef registered
        DESTRUCTIVE under a name absent from TOOL_TIERS must be
        denied in semi_auto (ask-deny — no approval UX yet), not
        silently allowed."""
        from astra.autonomy.manager import autonomy_manager
        from astra.runtime.agent_loop import _autonomy_check
        from astra.runtime.tool_registry import ActionTier as RegistryTier, ToolDef

        async def fn(args: dict) -> str:
            return "boom"

        td = ToolDef(
            name="local_bash",
            description="",
            input_schema={"type": "object"},
            fn=fn,
            tier=RegistryTier.DESTRUCTIVE,
        )
        previous = autonomy_manager.mode
        try:
            autonomy_manager.set_mode(AutonomyMode.SEMI_AUTO, reason="test")
            allowed, reason = _autonomy_check(td, "local_bash")
            assert allowed is False, (
                f"DESTRUCTIVE tool auto-allowed in semi_auto: {reason}"
            )
        finally:
            autonomy_manager.set_mode(previous, reason="test restore")


class TestManager:
    def test_default_mode(self):
        """Default mode comes from settings.default_autonomy_mode (.env)."""
        mgr = AutonomyManager()
        assert mgr.mode == AutonomyMode(settings.default_autonomy_mode)

    def test_set_mode(self):
        mgr = AutonomyManager()
        result = mgr.set_mode(AutonomyMode.SEMI_AUTO, reason="testing")
        assert result["to"] == "semi_auto"
        assert mgr.mode == AutonomyMode.SEMI_AUTO

    def test_time_based_revert(self):
        mgr = AutonomyManager()
        default_mode = mgr.mode  # Whatever .env says
        mgr.set_mode(AutonomyMode.FULL_AUTO, duration_minutes=0)
        # Duration is 0 minutes — force revert by backdating the revert_at
        mgr._revert_at = time.time() - 1
        assert mgr.mode == default_mode

    def test_task_based_revert(self):
        mgr = AutonomyManager()
        default_mode = mgr.mode  # Whatever .env says
        mgr.set_mode(AutonomyMode.FULL_AUTO, task_id="task-123")
        assert mgr.mode == AutonomyMode.FULL_AUTO

        reverted = mgr.complete_task("task-123")
        assert reverted is True
        assert mgr.mode == default_mode

    def test_task_complete_wrong_id_no_revert(self):
        mgr = AutonomyManager()
        mgr.set_mode(AutonomyMode.FULL_AUTO, task_id="task-123")
        reverted = mgr.complete_task("task-999")
        assert reverted is False
        assert mgr.mode == AutonomyMode.FULL_AUTO

    def test_history_tracked(self):
        mgr = AutonomyManager()
        mgr.set_mode(AutonomyMode.SEMI_AUTO)
        mgr.set_mode(AutonomyMode.FULL_AUTO)
        history = mgr.get_history()
        assert len(history) == 2

    def test_status(self):
        mgr = AutonomyManager()
        status = mgr.get_status()
        assert status["current_mode"] == settings.default_autonomy_mode
        assert status["time_remaining_minutes"] is None


class TestAuditLogger:
    def test_log_and_retrieve(self):
        logger = AuditLogger()
        logger.log(
            tool_name="Read",
            action_tier=ActionTier.READ,
            autonomy_mode=AutonomyMode.SEMI_AUTO,
            decision=PermissionDecision.ALLOW,
            tool_input_summary="file_path=/some/file.py",
        )
        entries = logger.get_entries()
        assert len(entries) == 1
        assert entries[0]["tool_name"] == "Read"
        assert entries[0]["decision"] == "allow"

    def test_filter_by_tool(self):
        logger = AuditLogger()
        logger.log("Read", ActionTier.READ, AutonomyMode.SEMI_AUTO, PermissionDecision.ALLOW)
        logger.log("Bash", ActionTier.DESTRUCTIVE, AutonomyMode.SEMI_AUTO, PermissionDecision.ASK)

        bash_entries = logger.get_entries(tool_name="Bash")
        assert len(bash_entries) == 1
        assert bash_entries[0]["tool_name"] == "Bash"

    def test_stats(self):
        logger = AuditLogger()
        logger.log("Read", ActionTier.READ, AutonomyMode.SEMI_AUTO, PermissionDecision.ALLOW)
        logger.log("Read", ActionTier.READ, AutonomyMode.SEMI_AUTO, PermissionDecision.ALLOW)
        logger.log("Bash", ActionTier.DESTRUCTIVE, AutonomyMode.SEMI_AUTO, PermissionDecision.ASK)

        stats = logger.get_stats()
        assert stats["total"] == 3
        assert stats["by_decision"]["allow"] == 2
        assert stats["by_decision"]["ask"] == 1

    def test_max_entries_trimmed(self):
        logger = AuditLogger(max_entries=5)
        for i in range(10):
            logger.log(f"Tool{i}", ActionTier.READ, AutonomyMode.FULL_AUTO, PermissionDecision.ALLOW)
        assert len(logger.get_entries(limit=100)) == 5


class TestDBPersistence:
    """Cross-service mode sync via the app_settings table.

    These cover the bug where the web /settings toggle wrote to
    app_settings but the stream service kept its in-memory mode —
    so users saw "semi_auto" in the UI while the agent enforced
    "always_ask".
    """

    @pytest.mark.asyncio
    async def test_refresh_from_db_updates_mode_when_changed(self):
        mgr = AutonomyManager()
        mgr.set_mode(AutonomyMode.ALWAYS_ASK)
        # Simulate the web UI having written "semi_auto" to app_settings.
        with patch(
            "astra.autonomy.manager._read_mode_from_db",
            return_value="semi_auto",
        ):
            changed = await mgr.refresh_from_db()
        assert changed is True
        assert mgr.mode == AutonomyMode.SEMI_AUTO

    @pytest.mark.asyncio
    async def test_refresh_from_db_noop_when_unchanged(self):
        mgr = AutonomyManager()
        mgr.set_mode(AutonomyMode.SEMI_AUTO)
        with patch(
            "astra.autonomy.manager._read_mode_from_db",
            return_value="semi_auto",
        ):
            changed = await mgr.refresh_from_db()
        assert changed is False
        assert mgr.mode == AutonomyMode.SEMI_AUTO

    @pytest.mark.asyncio
    async def test_refresh_from_db_tolerates_db_failure(self):
        """DB unreachable returns None — the manager keeps its current
        mode rather than dropping to a default. Turns must never fail
        because the config table is having a bad day."""
        mgr = AutonomyManager()
        mgr.set_mode(AutonomyMode.FULL_AUTO)
        with patch(
            "astra.autonomy.manager._read_mode_from_db", return_value=None
        ):
            changed = await mgr.refresh_from_db()
        assert changed is False
        assert mgr.mode == AutonomyMode.FULL_AUTO

    @pytest.mark.asyncio
    async def test_refresh_from_db_rejects_unknown_value(self):
        """A garbled value in the DB doesn't crash and doesn't promote
        the agent to a more permissive mode."""
        mgr = AutonomyManager()
        mgr.set_mode(AutonomyMode.ALWAYS_ASK)
        with patch(
            "astra.autonomy.manager._read_mode_from_db",
            return_value="god_mode",
        ):
            changed = await mgr.refresh_from_db()
        assert changed is False
        assert mgr.mode == AutonomyMode.ALWAYS_ASK

    @pytest.mark.asyncio
    async def test_set_mode_persisted_writes_to_db(self):
        """Agent-driven set_mode (via the set_mode tool) must persist
        to app_settings so the web UI reflects the change."""
        mgr = AutonomyManager()
        mgr.set_mode(AutonomyMode.ALWAYS_ASK)
        with patch(
            "astra.autonomy.manager._write_mode_to_db", return_value=True
        ) as write_mock:
            result = await mgr.set_mode_persisted(
                AutonomyMode.SEMI_AUTO, reason="test"
            )
        assert mgr.mode == AutonomyMode.SEMI_AUTO
        assert result["from"] == "always_ask"
        assert result["to"] == "semi_auto"
        write_mock.assert_called_once()
        # Verify the write got the right enum value
        assert write_mock.call_args[0][0] == AutonomyMode.SEMI_AUTO

    @pytest.mark.asyncio
    async def test_set_mode_persisted_still_succeeds_if_db_write_fails(self):
        """A failed DB write doesn't undo the in-memory change. The
        turn-level refresh on the next request will eventually resync
        — better to honour the explicit request locally."""
        mgr = AutonomyManager()
        mgr.set_mode(AutonomyMode.ALWAYS_ASK)
        with patch(
            "astra.autonomy.manager._write_mode_to_db", return_value=False
        ):
            await mgr.set_mode_persisted(AutonomyMode.FULL_AUTO)
        assert mgr.mode == AutonomyMode.FULL_AUTO
