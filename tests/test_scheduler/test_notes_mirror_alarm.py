"""The notes-mirror staleness alarm must prove itself before it pages.

While `notes.sync` is not wired in the executor the mirror cannot
advance whatever anyone does, so "mirror stale while the body is
polling" is permanently true the moment the laptop opens: a daily
push nobody can act on, the cries-wolf class
(learnings_monitor_must_prove_itself). The check now gates on the
catalogue mirror: unwired means report the age in the job result and
log at INFO; the first page is the first real one.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from astra.broker import client
from astra.scheduler import jobs

UTC = timezone.utc


def _run(coro):
    return asyncio.run(coro)


class _Session:
    """async_session stand-in: answers max(last_synced_at) then
    max(last_poll_at), on every call."""

    def __init__(self, last_sync, last_poll):
        self._pair = (last_sync, last_poll)
        self._vals = iter(())

    def __call__(self):
        self._vals = iter(self._pair)
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, stmt, params=None):
        v = next(self._vals)

        class R:
            def scalar(self_):
                return v
        return R()


def _setup(monkeypatch, *, mirror_age: timedelta, poll_age: timedelta,
           wired: bool):
    import astra.db.engine as _engine

    now = datetime.now(UTC)
    monkeypatch.setattr(_engine, "async_session",
                        _Session(now - mirror_age, now - poll_age))
    monkeypatch.setattr(jobs, "_NOTES_STALE_ALERTED_AT", None)
    verbs = (client.WIRED_VERBS | {"notes.sync"}) if wired \
        else (client.WIRED_VERBS - {"notes.sync"})
    monkeypatch.setattr(client, "WIRED_VERBS", frozenset(verbs))
    sent: list[dict] = []

    import astra.notifications as notifications

    def _notify(**kw):
        sent.append(kw)
        return True

    monkeypatch.setattr(notifications, "notify", _notify)
    return sent


def test_unwired_verb_suppresses_the_page_and_reports_the_age(monkeypatch):
    sent = _setup(monkeypatch, mirror_age=timedelta(hours=300),
                  poll_age=timedelta(seconds=20), wired=False)
    out = _run(jobs._notes_mirror_staleness_check())
    assert sent == [], "no push while nothing anyone does can change the outcome"
    assert out["mirror"] == "stale" and out["body"] == "polling"
    assert out["alerted"] == "suppressed: notes.sync unwired"
    assert out["age"] == "300h old"
    assert jobs._NOTES_STALE_ALERTED_AT is None


def test_wired_verb_with_a_stale_mirror_and_live_body_pages_once(monkeypatch):
    sent = _setup(monkeypatch, mirror_age=timedelta(hours=5),
                  poll_age=timedelta(seconds=20), wired=True)
    out = _run(jobs._notes_mirror_staleness_check())
    assert len(sent) == 1 and "5h old" in sent[0]["body"]
    assert out["alerted"] == "now"
    out = _run(jobs._notes_mirror_staleness_check())
    assert len(sent) == 1 and out["alerted"] == "already"


def test_a_closed_laptop_never_pages(monkeypatch):
    sent = _setup(monkeypatch, mirror_age=timedelta(hours=5),
                  poll_age=timedelta(hours=3), wired=True)
    out = _run(jobs._notes_mirror_staleness_check())
    assert sent == [] and "not polling" in out["body"]


def test_a_fresh_mirror_rearms(monkeypatch):
    monkeypatch.setattr(jobs, "_NOTES_STALE_ALERTED_AT", datetime.now(UTC))
    sent = _setup(monkeypatch, mirror_age=timedelta(minutes=10),
                  poll_age=timedelta(seconds=20), wired=True)
    out = _run(jobs._notes_mirror_staleness_check())
    assert sent == [] and out["mirror"] == "fresh"
    assert jobs._NOTES_STALE_ALERTED_AT is None


def test_gate_reads_the_catalogue_mirror_not_a_local_flag():
    import inspect

    src = inspect.getsource(jobs._notes_sync_wired)
    assert "client.WIRED_VERBS" in src
    assert "notes.sync" in src
