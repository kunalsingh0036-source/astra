"""Tests for the autonomy mode system."""

import time

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
