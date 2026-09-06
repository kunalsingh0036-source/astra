"""Watching an intent reads and only reads; the paged reader stays
inside its budget; the resolved-after-turn query has the shape both
its consumers rely on.

The old bridge's wait_for_result marked rows 'timeout' on the
courier's own patience, which made the cloud's deadline part of the
row's truth. store.wait_for_intent must never write, and a caller's
"I stopped waiting" must be exactly that. No database: the status
read is stubbed at the module attribute the watcher reaches it
through.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from astra.broker import client, store


def _run(coro):
    return asyncio.run(coro)


def _row(status: str, **over) -> dict:
    base = {
        "id": 3, "verb": "fs.read", "status": status, "deny_reason": None,
        "why": "t", "display_bytes": None, "receipt_bytes": None,
        "result_bytes": b"hello", "result_note": "", "created_at": None,
        "resolved_at": None, "expires_at": None, "session_claim": "turn:1",
    }
    base.update(over)
    return base


# ── wait_for_intent ───────────────────────────────────────


def test_wait_for_intent_source_contains_no_write():
    src = inspect.getsource(store.wait_for_intent).upper()
    for kw in ("UPDATE ", "INSERT ", "DELETE "):
        assert kw not in src, f"the watcher must never write ({kw.strip()})"


def test_wait_returns_the_row_once_terminal(monkeypatch):
    seq = iter([_row("pending"), _row("claimed"), _row("running"),
                _row("succeeded")])
    seen = 0

    async def _get(_id):
        nonlocal seen
        seen += 1
        return next(seq)

    monkeypatch.setattr(store, "get_intent_status", _get)
    row = _run(store.wait_for_intent(3, timeout_sec=5, poll_interval_sec=0.001))
    assert row["status"] == "succeeded" and seen == 4


def test_wait_returns_none_on_timeout_without_touching_the_row(monkeypatch):
    async def _get(_id):
        return _row("claimed")

    monkeypatch.setattr(store, "get_intent_status", _get)
    row = _run(store.wait_for_intent(3, timeout_sec=0.02, poll_interval_sec=0.005))
    assert row is None


def test_wait_returns_none_for_a_missing_row(monkeypatch):
    async def _get(_id):
        return None

    monkeypatch.setattr(store, "get_intent_status", _get)
    assert _run(store.wait_for_intent(3, timeout_sec=1)) is None


def test_run_intent_with_wait_returns_the_terminal_row(monkeypatch):
    async def _body():
        return 4, ""

    async def _live(_id):
        return store.BodyLiveness(4, "mbp", None, None, 5.0, False)

    async def _depth(_id):
        return 0

    async def _submit(**kw):
        return 3

    async def _wait(_id, *, timeout_sec, poll_interval_sec=0.5):
        assert timeout_sec == 7
        return _row("failed", result_bytes=b"exceeded max_runtime_ms (30000); "
                                            b"the action was abandoned")

    monkeypatch.setattr(client, "_only_body", _body)
    monkeypatch.setattr(store, "body_liveness", _live)
    monkeypatch.setattr(store, "pending_depth", _depth)
    monkeypatch.setattr(store, "submit_intent", _submit)
    monkeypatch.setattr(store, "wait_for_intent", _wait)
    r = _run(client.run_intent("fs.read", {"path": "/private/tmp/a"}, why="t",
                               actor="test", session_claim="turn:1", wait_sec=7))
    assert r.intent_id == 3 and r.status == "failed" and r.terminal
    assert "abandoned" in r.deny_reason


def test_run_intent_with_wait_reports_still_open_after_the_deadline(monkeypatch):
    async def _body():
        return 4, ""

    async def _live(_id):
        return store.BodyLiveness(4, "mbp", None, None, 5.0, False)

    async def _depth(_id):
        return 0

    async def _submit(**kw):
        return 3

    async def _wait(_id, *, timeout_sec, poll_interval_sec=0.5):
        return None

    async def _get(_id):
        return _row("claimed")

    monkeypatch.setattr(client, "_only_body", _body)
    monkeypatch.setattr(store, "body_liveness", _live)
    monkeypatch.setattr(store, "pending_depth", _depth)
    monkeypatch.setattr(store, "submit_intent", _submit)
    monkeypatch.setattr(store, "wait_for_intent", _wait)
    monkeypatch.setattr(store, "get_intent_status", _get)
    r = _run(client.run_intent("fs.read", {"path": "/private/tmp/a"}, why="t",
                               actor="test", session_claim="turn:1", wait_sec=1))
    assert r.intent_id == 3 and r.status == "claimed" and not r.terminal


# ── the paged reader ──────────────────────────────────────


def _page(n: int, status="succeeded", **over):
    return client.IntentResult(intent_id=n, status=status, verb="fs.read", **over)


def test_paged_read_stops_at_a_short_page_and_is_complete(monkeypatch):
    from astra.tools import reply_tools as rt

    full = b"a" * rt._READ_PAGE_BYTES
    offsets = []

    async def _ri(verb, args, **kw):
        offsets.append(args["offset"])
        assert args["limit"] == rt._READ_PAGE_BYTES == client.FS_READ_MAX_LIMIT
        data = full if len(offsets) < 3 else b"tail\n"
        return _page(len(offsets), result_bytes=data)

    monkeypatch.setattr(client, "run_intent", _ri)
    out = _run(rt._read_mac_file("/private/tmp/chat.txt"))
    assert out.complete and not out.error
    assert out.pages == 3 and out.resume_offset is None
    assert offsets == [0, rt._READ_PAGE_BYTES, 2 * rt._READ_PAGE_BYTES]
    assert len(out.data) == 2 * rt._READ_PAGE_BYTES + 5


def test_paged_read_is_bounded_per_turn_and_names_the_resume_offset(monkeypatch):
    from astra.tools import reply_tools as rt

    full = b"line\n" * (rt._READ_PAGE_BYTES // 5)
    filed = 0

    async def _ri(verb, args, **kw):
        nonlocal filed
        filed += 1
        return _page(filed, result_bytes=full)

    monkeypatch.setattr(client, "run_intent", _ri)
    out = _run(rt._read_mac_file("/private/tmp/big.txt"))
    assert not out.complete and not out.error
    assert filed == rt._READ_MAX_PAGES, "never more intents than the bound"
    assert out.pages == rt._READ_MAX_PAGES
    assert out.resume_offset == rt._READ_MAX_PAGES * rt._READ_PAGE_BYTES
    assert len(out.data) == rt._READ_MAX_PAGES * rt._READ_PAGE_BYTES, (
        "what was read is kept, never discarded"
    )
    assert rt._READ_MAX_PAGES < client.AUTO_INTENTS_PER_HOUR


def test_paged_read_treats_past_the_end_as_end_of_file(monkeypatch):
    from astra.tools import reply_tools as rt

    full = b"a" * rt._READ_PAGE_BYTES
    n = 0

    async def _ri(verb, args, **kw):
        nonlocal n
        n += 1
        if n == 1:
            return _page(1, result_bytes=full)
        return _page(2, status="failed",
                     deny_reason=f"offset {args['offset']} is past the end of "
                                 f"/private/tmp/x (size {args['offset']})")

    monkeypatch.setattr(client, "run_intent", _ri)
    out = _run(rt._read_mac_file("/private/tmp/x"))
    assert out.complete and not out.error and len(out.data) == rt._READ_PAGE_BYTES


def test_paged_read_resumes_from_an_offset(monkeypatch):
    from astra.tools import reply_tools as rt

    async def _ri(verb, args, **kw):
        assert args["offset"] == 1_310_720
        return _page(1, result_bytes=b"rest\n")

    monkeypatch.setattr(client, "run_intent", _ri)
    out = _run(rt._read_mac_file("/private/tmp/x", offset=1_310_720))
    assert out.complete and out.start == 1_310_720 and out.data == b"rest\n"


def test_paged_read_stops_when_a_page_is_still_open(monkeypatch):
    from astra.tools import reply_tools as rt

    async def _ri(verb, args, **kw):
        return _page(5, status="claimed")

    monkeypatch.setattr(client, "run_intent", _ri)
    out = _run(rt._read_mac_file("/private/tmp/x"))
    assert "still in progress" in out.error and "intent #5 claimed" in out.error


class _FakeClient:
    """httpx.AsyncClient stand-in: records the POST, answers ok."""
    posted: list[dict] = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None):
        _FakeClient.posted.append(json)

        class R:
            status_code = 200

            def json(self_):
                return {"ok": True, "parsed": 10, "new": 8, "duplicates": 2,
                        "channel_total": 100}
        return R()


def test_ingest_tool_ingests_the_partial_and_names_the_resume(monkeypatch):
    from astra.tools import reply_tools as rt

    _FakeClient.posted = []
    monkeypatch.setattr(rt.httpx, "AsyncClient", _FakeClient)

    async def _read(path, offset=0):
        return rt._ReadOutcome(start=0, data=b"one\ntwo\nthr", pages=4,
                               complete=False, resume_offset=11)

    monkeypatch.setattr(rt, "_read_mac_file", _read)
    out = _run(rt.ingest_voice_export_tool.handler({
        "channel": "whatsapp_personal", "path": "/private/tmp/c.txt",
        "self_name": "Kunal",
    }))
    text = out["content"][0]["text"]
    assert not out.get("is_error"), text
    assert _FakeClient.posted[0]["content"] == "one\ntwo\n", (
        "only complete lines are ingested from a partial read"
    )
    assert "PARTIAL read" in text and "offset=8" in text


def test_ingest_tool_refuses_a_partial_instagram_export_explicitly(monkeypatch):
    from astra.tools import reply_tools as rt

    _FakeClient.posted = []
    monkeypatch.setattr(rt.httpx, "AsyncClient", _FakeClient)

    async def _read(path, offset=0):
        return rt._ReadOutcome(start=0, data=b"{" * 100, pages=4,
                               complete=False, resume_offset=2_621_440)

    monkeypatch.setattr(rt, "_read_mac_file", _read)
    out = _run(rt.ingest_voice_export_tool.handler({
        "channel": "instagram", "path": "/private/tmp/dm.json",
        "self_name": "Kunal",
    }))
    text = out["content"][0]["text"]
    assert out.get("is_error") is True
    assert "Export too large for one turn" in text and "4 pages" in text
    assert _FakeClient.posted == [], "nothing half-parsed is ingested"


def test_ingest_tool_exposes_offset_to_the_model():
    import astra.runtime.tools  # noqa: F401
    from astra.runtime.tool_registry import REGISTRY

    td = REGISTRY.get("ingest_voice_export")
    assert td is not None
    assert "offset" in td.input_schema["properties"]
    assert "offset=" in td.description


# ── the resolved-after-turn query ─────────────────────────


def test_resolved_after_turn_query_joins_turn_end_and_reads_only():
    src = inspect.getsource(store.list_resolved_after_turn_end)
    assert "JOIN turns t ON i.session_claim = 'turn:' || CAST(t.id AS TEXT)" in src
    assert "i.resolved_at > t.ended_at" in src
    assert "t.ended_at IS NULL AND t.started_at < :dead_before" in src
    assert "(i.resolved_at, i.id) > (:ts, :since_id)" in src
    up = src.upper()
    for kw in ("UPDATE ", "INSERT ", "DELETE "):
        assert kw not in up


def test_resolved_after_turn_query_returns_the_status_row_shape():
    """Same columns as get_intent_status, so a row from either feeds
    client._from_row and the receipt verifier unchanged."""
    src = inspect.getsource(store.list_resolved_after_turn_end)
    assert "_STATUS_COLUMNS" in src, "one column list, shared by every status read"
    assert "_status_row(r)" in src
    cols = [c.strip() for c in store._STATUS_COLUMNS.split(",")]
    assert cols[:3] == ["id", "verb", "status"] and "resolved_at" in cols


@pytest.mark.parametrize("fn", [store.list_resolved_since,
                                store.list_resolved_after_turn_end])
def test_resolved_listings_are_in_the_public_api(fn):
    assert fn.__name__ in store.__all__


# ── the poll window is stated twice; it must not drift ───────────

def test_the_poll_window_is_the_same_number_in_both_modules():
    """store.sole_live_body inlines the 60 s window rather than
    importing it: client imports store, so an import here would be a
    cycle paid on every import. An inlined constant is a second fact,
    and a second fact is the class of bug this project keeps finding —
    so it is pinned rather than trusted."""
    import inspect

    from astra.broker import client, store

    src = inspect.getsource(store.sole_live_body)
    assert f"<= {client.BODY_POLL_WINDOW_SEC} " in src, (
        f"store.sole_live_body no longer uses "
        f"{client.BODY_POLL_WINDOW_SEC}s; the two windows have drifted")
