"""broker_notify pushes only for intents that resolved after the turn
that filed them had ended.

The first version selected every terminal `turn:%` intent, so a chat
ingest that paged four fs.read intents inside its turn produced four
pushes for reads the turn had already consumed. That trains the one
channel that will later carry signed-verb outcomes to be dismissed.
The scope now lives in ONE query, store.list_resolved_after_turn_end,
joined to turns.ended_at (a column production already has; no
migration).

Two layers:

  (a) the SQL, run for real against fixture rows on an in-memory
      sqlite (the statement is captured from the store function and
      executed unchanged, which is why the dead-turn cutoff is a bound
      parameter rather than now() arithmetic): a row resolved before
      its turn ended is NOT selected, one resolved after it IS, a
      dead turn counts as ended, a job-filed row and a non-terminal
      row never appear, and the high-water mark excludes what was
      already reported;
  (b) the job: one push per selected row, the mark advances, nothing
      is pushed when nothing was selected, and liveness is never a
      reason to push.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text

from astra.broker import store

UTC = timezone.utc
T0 = datetime(2026, 9, 6, 10, 0, 0, tzinfo=UTC)


def _run(coro):
    return asyncio.run(coro)


class _Capture:
    """An async_session stand-in that records the statement and
    parameters the store issues and returns no rows."""

    def __init__(self):
        self.sql = None
        self.params = None

    def __call__(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, stmt, params=None):
        self.sql, self.params = str(stmt), params

        class R:
            def fetchall(self_):
                return []
        return R()


def _captured_statement(monkeypatch, **kw):
    import astra.db.engine as _engine

    cap = _Capture()
    monkeypatch.setattr(_engine, "async_session", cap)
    _run(store.list_resolved_after_turn_end(**kw))
    assert cap.sql and cap.params is not None
    return cap.sql, cap.params


def _sqlite_with_fixtures():
    eng = create_engine("sqlite://")
    with eng.begin() as c:
        c.execute(text("""
            CREATE TABLE turns (id INTEGER PRIMARY KEY, started_at TEXT,
                                ended_at TEXT)"""))
        c.execute(text("""
            CREATE TABLE intents (
                id INTEGER PRIMARY KEY, verb TEXT, status TEXT,
                deny_reason TEXT, why TEXT, display_bytes BLOB,
                receipt_bytes BLOB, result_bytes BLOB, result_note TEXT,
                created_at TEXT, resolved_at TEXT, expires_at TEXT,
                session_claim TEXT)"""))
        turns = [
            # id, started, ended
            (1, T0, T0 + timedelta(minutes=2)),                 # normal, ended
            (2, T0 - timedelta(hours=1), None),                 # dead: never finalised
            (3, T0 + timedelta(minutes=30), None),              # still running
        ]
        for tid, st, en in turns:
            c.execute(text("INSERT INTO turns VALUES (:i, :s, :e)"),
                      {"i": tid, "s": str(st), "e": None if en is None else str(en)})
        intents = [
            # id, verb, status, claim, resolved
            (10, "fs.read", "succeeded", "turn:1", T0 + timedelta(minutes=1)),   # inside turn
            (11, "fs.write", "succeeded", "turn:1", T0 + timedelta(minutes=9)),  # after turn
            (12, "fs.edit", "denied", "turn:1", T0 + timedelta(minutes=12)),     # after turn
            (13, "fs.read", "succeeded", "turn:2", T0 - timedelta(minutes=30)),  # dead turn
            (14, "fs.read", "succeeded", "turn:3", T0 + timedelta(minutes=31)),  # turn open
            (15, "notes.sync", "succeeded", "job:notes_sync", T0 + timedelta(minutes=9)),
            (16, "fs.write", "awaiting_human", "turn:1", None),
            (17, "fs.read", "succeeded", "turn:abc-session", T0 + timedelta(minutes=9)),
        ]
        for iid, verb, st, claim, res in intents:
            c.execute(text("""
                INSERT INTO intents (id, verb, status, deny_reason, why,
                    result_note, created_at, resolved_at, session_claim)
                VALUES (:i, :v, :s, :d, 'why', '', :c, :r, :cl)"""),
                      {"i": iid, "v": verb, "s": st,
                       "d": "nope" if st == "denied" else None,
                       "c": str(T0), "r": None if res is None else str(res),
                       "cl": claim})
    return eng


def _select(monkeypatch, eng, **kw):
    sql, params = _captured_statement(monkeypatch, **kw)
    bound = {k: (str(v) if isinstance(v, datetime) else v)
             for k, v in params.items()}
    # The store computes the dead-turn cutoff from the wall clock; the
    # fixture turns are placed relative to T0, so anchor it there.
    bound["dead_before"] = str(T0 - timedelta(seconds=store.DEAD_TURN_AFTER_SEC))
    with eng.connect() as c:
        return [r[0] for r in c.execute(text(sql), bound).fetchall()]


def test_only_intents_resolved_after_their_turn_ended_are_selected(monkeypatch):
    eng = _sqlite_with_fixtures()
    ids = _select(monkeypatch, eng, ts=T0 - timedelta(days=1), limit=50)
    assert 10 not in ids, "resolved INSIDE its turn: consumed there, never pushed"
    assert 11 in ids and 12 in ids, "resolved after the turn ended: reported"
    assert 13 in ids, "a turn that died without ended_at counts as ended"
    assert 14 not in ids, "turn still running: not yet"
    assert 15 not in ids, "job-filed intents report through their job"
    assert 16 not in ids, "not terminal"
    assert 17 not in ids, "a CLI session claim has no turn row"
    assert ids == [13, 11, 12], "oldest first, resolved_at then id"


def test_high_water_mark_excludes_what_was_already_reported(monkeypatch):
    eng = _sqlite_with_fixtures()
    ids = _select(monkeypatch, eng, ts=T0 + timedelta(minutes=9), since_id=11,
                  limit=50)
    assert ids == [12]
    ids = _select(monkeypatch, eng, ts=T0 + timedelta(minutes=9), since_id=0,
                  limit=50)
    assert ids == [11, 12], "(ts, id) row comparison, not ts alone"


def test_newest_first_and_limit(monkeypatch):
    eng = _sqlite_with_fixtures()
    ids = _select(monkeypatch, eng, ts=T0 - timedelta(days=1), limit=2,
                  newest_first=True)
    assert ids == [12, 11]


# ── (b) the job ───────────────────────────────────────────


def _row(iid, verb, status, resolved, reason=None):
    return {"id": iid, "verb": verb, "status": status, "deny_reason": reason,
            "why": "t", "display_bytes": None, "receipt_bytes": None,
            "result_bytes": None, "result_note": "", "created_at": None,
            "resolved_at": resolved, "expires_at": None,
            "session_claim": "turn:1"}


def test_broker_notify_pushes_once_per_selected_row_and_advances_the_mark(monkeypatch):
    from astra.scheduler import jobs
    import astra.push as push

    asked: list[dict] = []
    pushed: list[dict] = []

    async def _list(ts, *, since_id=0, limit=50, newest_first=False):
        asked.append({"ts": ts, "since_id": since_id})
        return [_row(11, "fs.write", "succeeded", T0),
                _row(12, "fs.edit", "denied", T0 + timedelta(minutes=3), "nope")]

    async def _broadcast(*, title, body, url="/", tag=None, **k):
        pushed.append({"title": title, "body": body, "tag": tag})

    monkeypatch.setattr(store, "list_resolved_after_turn_end", _list)
    monkeypatch.setattr(push, "broadcast", _broadcast)
    monkeypatch.setattr(jobs, "_BROKER_NOTIFY_MARK", None)

    out = _run(jobs.broker_notify())
    assert out == {"status": "success", "notified": 2, "seen": 2}
    assert [p["body"] for p in pushed] == ["#11 fs.write succeeded",
                                           "#12 fs.edit denied: nope"]
    assert [p["tag"] for p in pushed] == ["intent-11", "intent-12"]
    assert jobs._BROKER_NOTIFY_MARK == (T0 + timedelta(minutes=3), 12)

    pushed.clear()

    async def _none(ts, *, since_id=0, limit=50, newest_first=False):
        asked.append({"ts": ts, "since_id": since_id})
        return []

    monkeypatch.setattr(store, "list_resolved_after_turn_end", _none)
    out = _run(jobs.broker_notify())
    assert out == {"status": "success", "notified": 0} and pushed == []
    assert asked[-1] == {"ts": T0 + timedelta(minutes=3), "since_id": 12}, (
        "the second run asks from the mark the first run set"
    )


def test_broker_notify_uses_the_shared_query_and_never_liveness():
    import inspect

    from astra.scheduler import jobs

    src = inspect.getsource(jobs.broker_notify)
    assert "list_resolved_after_turn_end" in src
    assert "last_poll_at" not in src and "bodies" not in src, (
        "liveness is never a reason to push (a closed laptop is normal)"
    )
    assert "SELECT" not in src, "the scope lives in the store, in one place"
