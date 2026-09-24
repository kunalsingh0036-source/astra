"""Offline monitor acceptance: no production DB, R2, or push gateway."""
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astra.broker import audit_anchor as A
from astra.broker import audit_monitor as M

NOW = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)
PIN = "ab" * 32


def observation(kind="verify", state="ok", verdict="intact", **kw):
    return M.Observation(kind, PIN, NOW, state, verdict, **kw)


@pytest.mark.parametrize("online,state", [(False, "deferred"), (True, "failed"), (None, "failed")])
@pytest.mark.parametrize("code", ["stale", "no_head"])
def test_sleep_is_not_a_fault_but_unknown_is_not_sleep(online, state, code):
    o = M.classify("freshness", PIN, A.Result(code, "", False), online=online, now=NOW)
    assert o.state == state
    assert (M.notification(o, None) is None) == (online is False)


@pytest.mark.parametrize("code", ["gap", "head_sig_invalid", "head_stamp_mismatch", "tampered"])
def test_integrity_failures_alert_even_with_the_lid_closed(code):
    o = M.classify("freshness", PIN, A.Result(code, "sensitive remote detail", False), online=False, now=NOW)
    assert o.state == "failed"
    assert "sensitive" not in M.notification(o, None)


def test_unchanged_alerts_are_quiet_and_recovery_is_explicit():
    failed = observation(state="failed", verdict="gap")
    assert M.notification(failed, None)
    assert M.notification(failed, failed.alert_key) is None
    healthy = observation()
    assert M.notification(healthy, None) is None
    assert M.notification(healthy, failed.alert_key)
    assert M.notification(healthy, "healthy") is None
    deferred = observation("freshness", "deferred", "stale")
    assert M.notification(deferred, failed.alert_key) is None


def test_malformed_result_never_becomes_a_success_or_leaks_data():
    o = M.classify("verify", PIN, A.Result("secret=https://key@host", "private", True), online=True, now=NOW)
    assert o.state == "failed" and o.verdict == "invalid_verifier_result"
    assert "secret" not in M.notification(o, None)


@pytest.mark.parametrize("kind", M.KINDS)
async def test_absent_configuration_is_reported_without_network(monkeypatch, kind):
    for key in ("AUDIT_R2_ENDPOINT", "AUDIT_R2_READ_KEY_ID", "AUDIT_R2_READ_SECRET", "AUDIT_CHAIN_ID", "AUDIT_BODY_ID"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(M, "_read", lambda *_: pytest.fail("must not read"))
    assert (await M.observe(kind)).state == "unconfigured"


def configured(monkeypatch):
    for key, value in {"AUDIT_R2_ENDPOINT": "https://example.invalid", "AUDIT_R2_READ_KEY_ID": "test",
                       "AUDIT_R2_READ_SECRET": "test", "AUDIT_CHAIN_ID": PIN, "AUDIT_BODY_ID": "42"}.items():
        monkeypatch.setenv(key, value)


async def test_freshness_queries_only_the_paired_body(monkeypatch):
    configured(monkeypatch)
    get = AsyncMock(return_value=SimpleNamespace(revoked=False, poll_age_sec=5))
    monkeypatch.setattr(M.store, "body_liveness", get)
    monkeypatch.setattr(M, "_read", lambda *_: A.Result("stale", "", False))
    assert (await M.observe("freshness")).state == "failed"
    get.assert_awaited_once_with(42)


async def test_reader_exception_is_sanitized(monkeypatch, caplog):
    configured(monkeypatch)
    def explode(*_):
        raise RuntimeError("Authorization: secret example.com/private-object")
    monkeypatch.setattr(M, "_read", explode)
    o = await M.observe("verify")
    assert o.verdict == "read_unavailable"
    assert "secret" not in caplog.text + str(asdict(o))


@pytest.mark.parametrize("delivered,marked", [(0, False), (1, True)])
async def test_push_must_be_delivered_before_checkpointing(monkeypatch, delivered, marked):
    monkeypatch.setattr(M, "observe", AsyncMock(return_value=observation(state="failed", verdict="gap")))
    monkeypatch.setattr(M, "save", AsyncMock(return_value={"notified_key": None}))
    mark = AsyncMock()
    monkeypatch.setattr(M, "mark_notified", mark)
    import astra.push.sender as sender
    monkeypatch.setattr(sender, "broadcast", AsyncMock(return_value=SimpleNamespace(delivered=delivered)))
    assert (await M.run_check("verify"))["status"] == "failed"
    assert bool(mark.await_count) == marked


async def test_database_failure_does_not_push_or_report_success(monkeypatch, caplog):
    monkeypatch.setattr(M, "observe", AsyncMock(return_value=observation()))
    monkeypatch.setattr(M, "save", AsyncMock(side_effect=RuntimeError("database password")))
    import astra.push.sender as sender
    push = AsyncMock()
    monkeypatch.setattr(sender, "broadcast", push)
    assert (await M.run_check("verify"))["verdict"] == "monitor_unavailable"
    push.assert_not_awaited()
    assert "password" not in caplog.text


async def test_health_query_timeout_reports_unavailable(monkeypatch):
    import asyncio
    async def blocked():
        await asyncio.sleep(60)
    monkeypatch.setattr(M, "snapshots", blocked)
    monkeypatch.setattr(M, "HEALTH_DB_TIMEOUT", 0.01)
    assert await M.health() == {"status": "failed", "reason": "monitor_unavailable"}


def health_rows():
    return [asdict(observation()), asdict(observation("freshness", verdict="fresh"))]


def test_health_requires_both_recent_checks_and_the_current_pin():
    rows = health_rows()
    assert M.health_report(rows, now=NOW, chain_id=PIN)["status"] == "ok"
    assert M.health_report([], now=NOW, chain_id=PIN)["status"] == "failed"
    assert M.health_report(rows[:1], now=NOW, chain_id=PIN)["status"] == "failed"
    assert M.health_report(rows, now=NOW, chain_id="cd" * 32)["status"] == "failed"
    assert M.health_report(rows, now=NOW + timedelta(minutes=21), chain_id=PIN)["status"] == "failed"
    assert M.health_report(rows, now=NOW - timedelta(seconds=1), chain_id=PIN)["status"] == "failed"


def test_full_check_can_expire_even_if_freshness_continues():
    rows = health_rows()
    rows[0]["checked_at"] -= timedelta(hours=27)
    assert M.health_report(rows, now=NOW, chain_id=PIN)["checks"]["verify"]["verdict"] == "check_overdue"


def test_sleep_is_explicitly_deferred_not_ok():
    rows = health_rows()
    rows[1].update(state="deferred", verdict="stale")
    assert M.health_report(rows, now=NOW, chain_id=PIN)["status"] == "deferred"


@pytest.mark.parametrize("status,http", [("ok", 200), ("deferred", 200), ("failed", 503)])
async def test_health_route_is_bounded_data_free_and_not_cached(monkeypatch, status, http):
    from services.stream.main import health_audit
    monkeypatch.setattr(M, "health", AsyncMock(return_value={"status": status}))
    response = await health_audit()
    assert response.status_code == http
    assert response.headers["cache-control"] == "no-store"


def test_both_jobs_are_registered_without_starting_scheduler_or_touching_db():
    from astra.scheduler.app import _build_scheduler
    scheduler = _build_scheduler()
    jobs = {job.id: job for job in scheduler.get_jobs()}
    assert jobs["audit_verify"].func is M.run_audit_verify
    assert jobs["audit_freshness"].func is M.run_audit_freshness
    assert jobs["audit_verify"].trigger.interval.total_seconds() == 86400
    assert jobs["audit_freshness"].trigger.interval.total_seconds() == 600
    for kind in M.KINDS:
        job = jobs[f"audit_{kind}"]
        assert job.max_instances == 1 and job.coalesce
        assert 0 < (job.next_run_time - datetime.now(timezone.utc)).total_seconds() <= 60


def test_monitor_has_no_delete_probe_or_credentials_in_snapshot():
    import inspect
    src = inspect.getsource(M)
    assert "delete_probe(" not in src and "delete_object(" not in src
    assert set(asdict(observation())) == {"check_kind", "chain_id", "checked_at", "state", "verdict", "record_count", "head_seq"}
