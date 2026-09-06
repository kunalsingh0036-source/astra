"""The "intents resolved since your last turn" block exists, and the
tool description that promises it tells the truth.

submit_intent's description told the model that anything resolving
after the turn ended would be listed at the start of the next one;
nothing built that block, so the model either invented an outcome or
said nothing arrived (the confabulation class in
learnings_astra_confabulation.md). astra/context/now.py now carries
it, fed by the same store query as the completion push.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from astra.broker import store
from astra.context import now as kn

UTC = timezone.utc


def _run(coro):
    return asyncio.run(coro)


def _row(iid, verb, status, age: timedelta, reason=None):
    return {"id": iid, "verb": verb, "status": status, "deny_reason": reason,
            "resolved_at": datetime.now(UTC) - age, "session_claim": "turn:1"}


def test_intents_line_lists_newest_first_with_age_and_reason(monkeypatch):
    asked = {}

    async def _list(ts, *, since_id=0, limit=50, newest_first=False):
        asked.update(ts=ts, limit=limit, newest_first=newest_first)
        return [_row(12, "fs.edit", "denied", timedelta(minutes=3, seconds=30), "user declined"),
                _row(11, "fs.write", "succeeded", timedelta(hours=1, minutes=5, seconds=30))]

    monkeypatch.setattr(store, "list_resolved_after_turn_end", _list)
    line = _run(kn._intents_line())
    assert line.startswith("🖥 Mac")
    assert "#12 fs.edit denied (3m ago): user declined" in line
    assert "#11 fs.write succeeded (1h05m ago)" in line
    assert "user declined" not in line.split("#11")[1]
    assert asked["newest_first"] is True and asked["limit"] == kn._INTENTS_SHOWN
    assert datetime.now(UTC) - asked["ts"] <= kn._INTENTS_WINDOW + timedelta(seconds=5)


def test_intents_line_is_empty_when_nothing_resolved(monkeypatch):
    async def _none(ts, *, since_id=0, limit=50, newest_first=False):
        return []

    monkeypatch.setattr(store, "list_resolved_after_turn_end", _none)
    assert _run(kn._intents_line()) == ""


def test_block_carries_the_line_and_drops_it_when_the_source_fails(monkeypatch):
    async def _empty():
        return ""

    for src in ("_calendar_line", "_inbox_line", "_training_line", "_focus_lines"):
        monkeypatch.setattr(kn, src, _empty)

    async def _list(ts, *, since_id=0, limit=50, newest_first=False):
        return [_row(11, "fs.write", "succeeded", timedelta(minutes=2))]

    monkeypatch.setattr(store, "list_resolved_after_turn_end", _list)
    monkeypatch.setitem(kn._cache, "block", None)
    monkeypatch.setitem(kn._cache, "ts", 0.0)
    block = _run(kn.build_kunal_now())
    assert "<kunal_now" in block and "#11 fs.write succeeded" in block

    async def _boom(ts, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(store, "list_resolved_after_turn_end", _boom)
    monkeypatch.setitem(kn._cache, "block", None)
    monkeypatch.setitem(kn._cache, "ts", 0.0)
    assert _run(kn.build_kunal_now()) == "", "a dead source drops its line, never the turn"


def test_submit_intent_description_promises_only_what_the_block_delivers():
    import astra.runtime.tools  # noqa: F401
    from astra.runtime.tool_registry import REGISTRY

    d = REGISTRY.get("submit_intent").description
    assert "<kunal_now>" in d and "AFTER this turn has ended" in d
    assert "finished inside the turn is not repeated" in d
    assert f"last {int(kn._INTENTS_WINDOW.total_seconds() // 3600)} hours" in d
    assert "start of the next one" not in d, "the old, unkept promise"
