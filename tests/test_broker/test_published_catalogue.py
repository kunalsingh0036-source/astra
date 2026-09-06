"""Which source decides whether a verb is wired.

Two sources can say what the executor on Kunal's Mac performs: the
broker's publish to /broker/catalog (the executor's own word, relayed,
when a build puts a per-verb `wired` flag in the POST) and the mirror in
astra/broker/client.py (pinned to the Swift sources by
test_catalogue_mirror.py). The publish wins when this process has it;
the mirror is the fallback for the scheduler (a separate process the
POST never reaches), for a stream process restarted after the broker's
last publish, and for a broker build that publishes no flag.

Both drift directions are pinned: a verb the publish wires and the
mirror lacks is FILED (the Mac would refuse a wired verb on a stale
mirror otherwise), a verb the publish unwires and the mirror has is
REFUSED before filing (no Touch ID prompt for an executor refusal).
Neither class files blindly: the scheduler never has the publish and
would otherwise file a doomed notes.sync every 30 minutes.

No database. Store functions are stubbed at the module attribute the
client reaches them through.
"""

from __future__ import annotations

import ast
import asyncio
import pathlib

import pytest

from astra.broker import client, store

_ROOT = "/Users/kunalsingh/Documents"
_REPO = pathlib.Path(__file__).resolve().parents[2]


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _no_publish(monkeypatch):
    """Every test starts as a process the broker has not published to."""
    monkeypatch.setattr(client, "_PUBLISHED", {})


def _verbs(wired: dict[str, bool] | None) -> list[dict]:
    """A /broker/catalog body: every catalogue verb, with `wired` on the
    verbs `wired` names (None: the pre-flag broker build)."""
    out = []
    for v in client.CATALOGUE:
        d: dict = {"name": v.name, "policy": v.policy}
        if wired is not None and v.name in wired:
            d["wired"] = wired[v.name]
        out.append(d)
    return out


def _all(wired_names: set[str]) -> dict[str, bool]:
    return {v.name: (v.name in wired_names) for v in client.CATALOGUE}


@pytest.fixture
def filed(monkeypatch):
    calls: list[dict] = []

    async def _submit(**kw):
        calls.append(kw)
        return 42

    async def _boom(*a, **k):
        raise AssertionError("must not touch the database for this refusal")

    monkeypatch.setattr(store, "submit_intent", _submit)
    monkeypatch.setattr(store, "body_liveness", _boom)
    monkeypatch.setattr(store, "pending_depth", _boom)
    monkeypatch.setattr(client, "_only_body", _boom)
    return calls


def _with_body(monkeypatch, *, age=12.0, depth=0):
    async def _body():
        return 4, ""

    async def _live(_id):
        return store.BodyLiveness(body_id=4, label="kunal-mbp", last_poll_at=None,
                                  last_completed_at=None, poll_age_sec=age,
                                  revoked=False)

    async def _depth(_id):
        return depth

    monkeypatch.setattr(client, "_only_body", _body)
    monkeypatch.setattr(store, "body_liveness", _live)
    monkeypatch.setattr(store, "pending_depth", _depth)


# ── which source decides ──────────────────────────────────


def test_no_publish_means_the_mirror_decides_every_verb():
    assert client.published_wired() is None
    for v in client.CATALOGUE:
        assert client.wired_state(v.name) == (v.wired, "mirror"), v.name


def test_a_full_publish_decides_every_verb():
    pub = client.note_published_catalogue(
        4, _verbs(_all({"fs.read", "fs.glob", "fs.grep"})), "cd" * 20,
    )
    assert pub.wired == {"fs.read", "fs.glob", "fs.grep"}
    assert pub.verbs == set(client.CATALOGUE_BY_NAME)
    assert client.published_wired() is pub
    assert client.published_wired(4) is pub
    assert client.wired_state("fs.grep") == (True, "published")
    assert client.wired_state("fs.read") == (True, "published")
    assert client.wired_state("notes.sync") == (False, "published")
    assert client.wired_state("fs.write") == (False, "published")


def test_a_publish_without_flags_leaves_the_mirror_in_charge():
    """Today's broker build: CloudClient.publishCatalog sends no
    `wired`. The publish is recorded (display) but decides nothing."""
    pub = client.note_published_catalogue(4, _verbs(None), "")
    assert pub.wired is None and pub.verbs
    assert client.published_wired() is None
    assert client.wired_state("fs.grep") == (False, "mirror")
    assert client.wired_state("fs.read") == (True, "mirror")


def test_a_partial_publish_is_treated_as_no_flag(caplog):
    """Half a set is not a set: flags on some verbs only are ignored,
    loudly, rather than read as 'everything else is unwired'."""
    with caplog.at_level("WARNING", logger="astra.broker.client"):
        pub = client.note_published_catalogue(4, _verbs({"fs.read": True}), "")
    assert pub.wired is None
    assert client.published_wired() is None
    assert any("ignoring the flags" in r.getMessage() for r in caplog.records)
    assert client.wired_state("fs.read") == (
        client.CATALOGUE_BY_NAME["fs.read"].wired, "mirror",
    )


def test_a_non_boolean_flag_does_not_count():
    verbs = _verbs(_all({"fs.read"}))
    next(v for v in verbs if v["name"] == "fs.glob")["wired"] = "yes"
    assert client.note_published_catalogue(4, verbs).wired is None


def test_two_published_bodies_fall_back_to_the_mirror_without_an_id():
    """run_intent checks before it knows the body; with two publishes
    it must not guess. With the id, that body's publish decides."""
    client.note_published_catalogue(4, _verbs(_all({"fs.read"})))
    client.note_published_catalogue(5, _verbs(_all({"fs.grep"})))
    assert client.published_wired() is None
    assert client.published_wired(5).wired == {"fs.grep"}
    assert client.wired_state("fs.grep") == (False, "mirror")
    assert client.wired_state("fs.grep", 5) == (True, "published")
    assert client.wired_state("fs.grep", 4) == (False, "published")


def test_a_later_publish_replaces_the_earlier_one():
    client.note_published_catalogue(4, _verbs(_all({"fs.read"})))
    client.note_published_catalogue(4, _verbs(_all({"fs.read", "fs.grep"})))
    assert client.published_wired().wired == {"fs.read", "fs.grep"}


# ── run_intent honours it, both drift directions ──────────


def test_run_intent_files_a_verb_the_publish_wires_though_the_mirror_does_not(
    filed, monkeypatch,
):
    _with_body(monkeypatch)
    assert "fs.grep" not in client.WIRED_VERBS, "pick a verb the mirror lacks"
    client.note_published_catalogue(4, _verbs(_all({"fs.read", "fs.glob", "fs.grep"})))
    r = _run(client.run_intent("fs.grep", {"pattern": "x", "path": _ROOT},
                               why="t", actor="test", session_claim="turn:1"))
    assert r.filed and r.intent_id == 42 and r.status == "pending"
    assert [c["verb"] for c in filed] == ["fs.grep"]


def test_run_intent_refuses_a_verb_the_publish_unwires_though_the_mirror_has_it(filed):
    assert "fs.read" in client.WIRED_VERBS, "pick a verb the mirror has"
    client.note_published_catalogue(4, _verbs(_all({"fs.glob"})))
    r = _run(client.run_intent("fs.read", {"path": _ROOT + "/a"}, why="t",
                               actor="test", session_claim="turn:1"))
    assert r.refused and r.refusal_code == "unwired"
    assert filed == []
    assert "last publish" in (r.deny_reason or "")


def test_a_signed_verb_is_refused_on_the_publish_before_any_database_call(filed):
    """The whole point: no Touch ID prompt for a verb the executor will
    refuse. The `filed` fixture makes every DB reach raise."""
    client.note_published_catalogue(4, _verbs(_all({"fs.read", "fs.glob"})))
    r = _run(client.run_intent("fs.write", {"path": _ROOT + "/a", "content": "x"},
                               why="t", actor="test", session_claim="turn:1"))
    assert r.refusal_code == "unwired" and filed == []


def test_a_signed_verb_the_publish_wires_reaches_the_body_checks(filed, monkeypatch):
    _with_body(monkeypatch)
    client.note_published_catalogue(
        4, _verbs(_all({"fs.read", "fs.glob", "fs.write"})),
    )
    r = _run(client.run_intent("fs.write", {"path": _ROOT + "/a", "content": "x"},
                               why="t", actor="test", session_claim="turn:1"))
    assert r.filed
    assert filed[0]["ttl_seconds"] == client.TTL_SIGNED_SEC


def test_the_mirror_refusal_text_does_not_cite_a_publish(filed):
    r = _run(client.run_intent("fs.grep", {"pattern": "x", "path": _ROOT},
                               why="t", actor="test", session_claim="turn:1"))
    assert r.refusal_code == "unwired"
    assert "last publish" not in (r.deny_reason or "")


# ── the route hands the publish over; body_status says which decided ─


def test_the_catalog_route_hands_the_publish_to_the_client():
    """services/stream/main.py is not importable in tests (it wires the
    app at import), so the route is read as code: broker_catalog must
    call note_published_catalogue, or the flag is stored for display
    and never decides anything."""
    src = (_REPO / "services/stream/main.py").read_text()
    tree = ast.parse(src)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "broker_catalog"
    )
    called = {
        n.func.attr for n in ast.walk(fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }
    assert "note_published_catalogue" in called


def test_body_status_reports_which_source_decided(monkeypatch):
    from astra.runtime.tools import physical

    async def _body():
        return 4, ""

    async def _live(_id):
        return store.BodyLiveness(body_id=4, label="kunal-mbp", last_poll_at=None,
                                  last_completed_at=None, poll_age_sec=12.0,
                                  revoked=False)

    monkeypatch.setattr(client, "_only_body", _body)
    monkeypatch.setattr(store, "body_liveness", _live)

    text = _run(physical.body_status_impl({}))["content"][0]["text"]
    assert "from the cloud mirror" in text
    grep_line = next(ln for ln in text.splitlines() if ln.startswith("- fs.grep("))
    assert "NOT wired" in grep_line

    client.note_published_catalogue(
        4, _verbs(_all({"fs.read", "fs.glob", "fs.grep"})), "deadbeefcafe0000",
    )
    text = _run(physical.body_status_impl({}))["content"][0]["text"]
    assert "as the broker published it to this process" in text
    assert "deadbeefcafe" in text
    grep_line = next(ln for ln in text.splitlines() if ln.startswith("- fs.grep("))
    assert "NOT wired" not in grep_line and grep_line.endswith("wired")
    read_line = next(ln for ln in text.splitlines() if ln.startswith("- fs.read("))
    assert read_line.endswith("wired")


def test_the_submit_description_is_rendered_from_the_mirror_only():
    """The description is registered once at import; a publish that
    arrives later must not be able to change it silently (the test
    above pins body_status as the live view)."""
    from astra.runtime.tools import physical

    client.note_published_catalogue(4, _verbs(_all(set())))
    assert "- fs.read(path, limit?, offset?): no fingerprint; wired" in physical._SUBMIT_DESCRIPTION
