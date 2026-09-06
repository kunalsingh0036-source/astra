"""Refusals are codes, not sentences.

The intent client refuses before filing for eight distinct reasons,
and three callers (the notes chat tool, the scheduler's notes_sync
job, and ingest_voice_export's paged reader) must each say the right
thing for each. The first version matched `"not wired" in reason`,
which is the EXECUTOR's phrase; the client's own refusal said "cannot
perform ... yet", so the unwired branch was dead and the chat tool
told Kunal to open a laptop that was already open and polling.

Three properties, each pinned here:

  (a) every pre-filing refusal carries a `refusal_code` from
      REFUSAL_CODES, no row is filed, and the database is not
      touched for the refusals that need no database;
  (b) every caller branches on the code: an unwired verb never
      produces laptop advice, an offline body always does, an
      enrolment problem is never dressed as a closed laptop;
  (c) executor text is matched ONLY on the deny_reason of a FILED
      row, and a failed row's executor error reaches the caller
      (client._from_row copies result_bytes into deny_reason, the
      way ServeLoop.swift does for a refusal).

No database. Store functions are stubbed at the module attribute the
client reaches them through.
"""

from __future__ import annotations

import asyncio

import pytest

from astra.broker import client, store

_ROOT = "/Users/kunalsingh/Documents"


def _run(coro):
    return asyncio.run(coro)


def _fresh(body_id: int = 4, age: float = 12.0) -> store.BodyLiveness:
    return store.BodyLiveness(body_id=body_id, label="kunal-mbp",
                              last_poll_at=None, last_completed_at=None,
                              poll_age_sec=age, revoked=False)


@pytest.fixture
def filed(monkeypatch):
    """Record submit_intent calls; make the DB-touching lookups fail
    loudly unless a test replaces them."""
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
        return _fresh(age=age)

    async def _depth(_id):
        return depth

    monkeypatch.setattr(client, "_only_body", _body)
    monkeypatch.setattr(store, "body_liveness", _live)
    monkeypatch.setattr(store, "pending_depth", _depth)


# ── (a) codes, no row, no DB ──────────────────────────────


def test_refusal_codes_are_a_closed_set():
    assert client.REFUSAL_CODES == {
        "unknown_verb", "args", "signed_outside_turn", "unwired",
        "no_body", "ambiguous_body", "offline", "busy", "queue_full",
    }


@pytest.mark.parametrize("verb,args,claim,code", [
    ("fs.nope", {"path": _ROOT + "/a"}, "turn:1", "unknown_verb"),
    ("", {}, "turn:1", "args"),
    ("fs.read", {"path": "relative.md"}, "turn:1", "args"),
    ("fs.read", {"path": _ROOT + "/a", "approved": True}, "turn:1", "args"),
    ("fs.write", {"path": _ROOT + "/a", "content": "x"}, "", "signed_outside_turn"),
    ("fs.grep", {"pattern": "x", "path": _ROOT}, "turn:1", "unwired"),
    ("notes.sync", {}, "", "unwired"),
])
def test_pre_database_refusals_carry_a_code_and_file_nothing(
    filed, verb, args, claim, code,
):
    r = _run(client.run_intent(verb, args, why="t", actor="test",
                               session_claim=claim))
    assert r.status == "refused" and r.refused
    assert r.refusal_code == code
    assert r.refusal_code in client.REFUSAL_CODES
    assert r.intent_id is None and not r.filed
    assert filed == []


def test_why_missing_is_an_args_refusal(filed):
    r = _run(client.run_intent("fs.read", {"path": _ROOT + "/a"}, why="",
                               actor="test", session_claim="turn:1"))
    assert r.refusal_code == "args" and filed == []


def test_no_body_and_ambiguous_body_are_distinct_codes(filed, monkeypatch):
    async def _none():
        return None, client._BodyRefusal("No body is registered", "no_body")

    monkeypatch.setattr(client, "_only_body", _none)
    r = _run(client.run_intent("fs.read", {"path": _ROOT + "/a"}, why="t",
                               actor="test", session_claim="turn:1"))
    assert r.refusal_code == "no_body" and filed == []

    async def _two():
        return None, client._BodyRefusal("More than one body", "ambiguous_body")

    monkeypatch.setattr(client, "_only_body", _two)
    r = _run(client.run_intent("fs.read", {"path": _ROOT + "/a"}, why="t",
                               actor="test", session_claim="turn:1"))
    assert r.refusal_code == "ambiguous_body" and filed == []


def test_a_plain_string_body_reason_counts_as_no_body(filed, monkeypatch):
    """Compatibility with the shape test stubs use: (None, "text")."""
    async def _plain():
        return None, "No body is registered, so there is nothing to carry this out."

    monkeypatch.setattr(client, "_only_body", _plain)
    r = _run(client.run_intent("fs.read", {"path": _ROOT + "/a"}, why="t",
                               actor="test", session_claim="turn:1"))
    assert r.refusal_code == "no_body"
    assert "No body is registered" in (r.deny_reason or "")


def test_offline_body_is_refused_in_one_round_trip(filed, monkeypatch):
    _with_body(monkeypatch, age=client.BODY_POLL_WINDOW_SEC + 1)
    r = _run(client.run_intent("fs.read", {"path": _ROOT + "/a"}, why="t",
                               actor="test", session_claim="turn:1"))
    assert r.refusal_code == "offline" and filed == []
    assert "laptop closed is normal" in (r.deny_reason or "")


def test_full_queue_is_refused(filed, monkeypatch):
    _with_body(monkeypatch, depth=client.MAX_PENDING)
    r = _run(client.run_intent("fs.read", {"path": _ROOT + "/a"}, why="t",
                               actor="test", session_claim="turn:1"))
    assert r.refusal_code == "queue_full" and filed == []


def test_a_filed_intent_has_no_refusal_code(filed, monkeypatch):
    _with_body(monkeypatch)
    r = _run(client.run_intent("fs.read", {"path": _ROOT + "/a"}, why="t",
                               actor="test", session_claim="turn:1"))
    assert r.filed and r.intent_id == 42
    assert r.status == "pending" and r.refusal_code == ""
    assert len(filed) == 1 and filed[0]["session_claim"] == "turn:1"


# ── the canonicaliser mirror ──────────────────────────────


_READ = client.CATALOGUE_BY_NAME["fs.read"]
_GLOB = client.CATALOGUE_BY_NAME["fs.glob"]


@pytest.mark.parametrize("spec,args,needle", [
    (_READ, {"path": "README.md"}, "absolute"),
    (_READ, {"path": "/Users/kunalsingh/.ssh/id_rsa"}, "secret or state store"),
    (_READ, {"path": _ROOT + "/x/.env.local"}, "environment file"),
    (_READ, {"path": _ROOT + "/gmail_token.json"}, "named like a credential"),
    (_READ, {"path": _ROOT + "/server.pem.bak"}, "key material"),
    (_READ, {"path": "/etc/passwd"}, "outside every compiled root"),
    (_READ, {"path": _ROOT + "/../.ssh/x"}, "secret or state store"),
    (_READ, {"path": _ROOT + "/../../../etc/hosts"}, "outside every compiled root"),
    (_READ, {"path": None}, "unusable type NoneType"),
    (_READ, {"path": ["/x"]}, "unusable type list"),
    (_READ, {"path": _ROOT + "/a", "limit": 700000}, "655360-byte ceiling"),
    (_READ, {"path": _ROOT + "/a", "limit": 1.5}, "float"),
    (_READ, {"path": _ROOT + "/a", "offset": -1}, "non-negative"),
    (_GLOB, {"pattern": "/Users/kunalsingh/Claude Code/../.ssh/*"}, "'..'"),
    (_GLOB, {"pattern": "*.md"}, "absolute"),
    (_GLOB, {"pattern": "/*.md"}, "no literal directory prefix"),
    (_GLOB, {"pattern": "/Users/kunalsingh/*.md"}, "outside every compiled root"),
    (_GLOB, {"pattern": _ROOT + "/.env*"}, "environment files"),
    (_GLOB, {"pattern": _ROOT + "/x", "root": _ROOT, "extra": 1}, "not an argument"),
])
def test_validate_args_refuses_what_the_canonicaliser_refuses(spec, args, needle):
    with pytest.raises(client.ArgsInvalid) as e:
        client.validate_args(spec, args)
    assert needle in str(e.value), str(e.value)
    assert "Nothing was filed" in str(e.value)


@pytest.mark.parametrize("spec,args", [
    (_READ, {"path": _ROOT + "/exports/chat.txt"}),
    (_READ, {"path": "/tmp/fixture.txt"}),                  # /tmp == /private/tmp
    (_READ, {"path": "/private/tmp/fixture.txt", "offset": 655360,
             "limit": client.FS_READ_MAX_LIMIT}),
    (_READ, {"path": "/Users/kunalsingh/Claude Code/astra/astra/tools/secrets.ts"}),
    (_GLOB, {"pattern": _ROOT + "/**/*.md"}),
    (_GLOB, {"pattern": "/tmp/astra-parity/*"}),
])
def test_validate_args_accepts_what_the_canonicaliser_accepts(spec, args):
    client.validate_args(spec, args)


def test_size_cap_is_measured_the_way_the_broker_measures_it():
    """Foundation's JSONSerialization escapes '/' as '\\/' and keeps
    UTF-8; ACE-1 bytes are smaller. Measuring ACE let an intent pass
    here and be refused as tooLarge on the Mac."""
    n = client._broker_measured_bytes({"path": "/a/b", "x": "é"})
    assert n == len('{"path":"\\/a\\/b","x":"é"}'.encode("utf-8"))
    big = {"path": _ROOT + "/a", "content": "x" * 1_048_570}
    with pytest.raises(client.ArgsInvalid, match="as the broker measures"):
        client.validate_args(client.CATALOGUE_BY_NAME["fs.write"], big)


# ── (c) a failed row's executor error reaches the caller ──


def _row(**over) -> dict:
    base = {
        "id": 5, "verb": "fs.read", "status": "failed", "deny_reason": None,
        "why": "t", "display_bytes": None, "receipt_bytes": None,
        "result_bytes": b"cannot read /private/tmp/fifo: not a regular file",
        "result_note": "", "created_at": None, "resolved_at": None,
        "expires_at": None, "session_claim": "turn:1",
    }
    base.update(over)
    return base


def test_failed_row_surfaces_the_executor_error_as_deny_reason():
    r = client._from_row(_row())
    assert r.status == "failed"
    assert r.deny_reason == "cannot read /private/tmp/fifo: not a regular file"
    assert r.refusal_code == ""


def test_failed_row_fallback_is_cut_at_400_and_never_overrides_a_broker_reason():
    r = client._from_row(_row(result_bytes=b"x" * 1000))
    assert len(r.deny_reason) == 400
    r = client._from_row(_row(deny_reason="executor_error: broker said so"))
    assert r.deny_reason == "executor_error: broker said so"
    r = client._from_row(_row(result_bytes=b"\xff\xfe\x00binary"))
    assert "not text" in r.deny_reason
    r = client._from_row(_row(status="succeeded", result_bytes=b"payload"))
    assert r.deny_reason is None


def test_poll_status_prints_the_executor_error_for_a_failed_row(monkeypatch):
    from astra.runtime.tools import physical

    async def _status(_id):
        return client._from_row(_row())

    monkeypatch.setattr(client, "status", _status)
    out = _run(physical.poll_status_impl({"intent_id": 5}))
    text = out["content"][0]["text"]
    assert "failed" in text
    assert "not a regular file" in text
    assert "(none given)" not in text


# ── (b) every caller branches on the code ─────────────────


def _refusal(code: str, verb: str = "notes.sync") -> client.IntentResult:
    words = {
        "unwired": "the body cannot perform notes.sync yet; nothing was filed",
        "offline": "body offline (laptop closed is normal): Kunal's Mac last "
                   "polled 900 s ago, outside the 60 s window",
        "busy": "body busy: Kunal's Mac is up but occupied with intent #7 "
                "(fs.read, running: still executing). The broker serves one "
                "intent at a time and does not poll while inside one.",
        "no_body": "No body is registered, so there is nothing to carry this out.",
        "ambiguous_body": "More than one body is registered.",
        "queue_full": "20 intents are already pending on Kunal's Mac.",
        "args": "args.path is not an argument of notes.sync.",
        "signed_outside_turn": "needs Kunal's fingerprint and this call is not inside a turn",
        "unknown_verb": "'x' is not a catalogue verb.",
    }[code]
    return client.IntentResult(intent_id=None, status="refused", verb=verb,
                               deny_reason=words, refusal_code=code)


_LAPTOP_ADVICE = ("open the laptop", "when the mac is open", "closed laptop",
                  "retry when", "wake")


@pytest.mark.parametrize("code", sorted(client.REFUSAL_CODES))
def test_notes_chat_tool_gives_a_remedy_that_can_work(monkeypatch, code):
    from astra.tools import notes_tools

    async def _sync(*, why, wait_sec):
        return _refusal(code)

    async def _last():
        return "2026-09-05 12:47 UTC"

    monkeypatch.setattr(notes_tools, "sync_via_body", _sync)
    monkeypatch.setattr(notes_tools, "_last_synced_at", _last)
    out = _run(notes_tools._sync_via_body_for_chat())
    text = out["content"][0]["text"]
    low = text.lower()
    assert out.get("is_error") is True
    assert "2026-09-05 12:47 UTC" in text, "the mirror's age is always stated"
    if code == "unwired":
        assert "gui" in low and "not wired" in low
        assert not any(p in low for p in _LAPTOP_ADVICE), text
        assert "changes nothing" in low
    elif code == "offline":
        assert "closed laptop" in low and "retry when the mac is open" in low
    elif code == "busy":
        # The remedy that can work is waiting for the open intent, not
        # touching the laptop: it is open, and the broker is using it.
        assert "busy" in low and "#7" in text
        assert not any(p in low for p in _LAPTOP_ADVICE), text
    elif code in ("no_body", "ambiguous_body"):
        assert "enrolment" in low and "do not tell kunal to open it" in low
    else:
        assert "closed laptop" not in low or "not a closed laptop" in low


@pytest.mark.parametrize("code", sorted(client.REFUSAL_CODES))
def test_notes_sync_job_maps_each_refusal_to_skipped_with_its_code(code):
    from astra.scheduler import jobs

    out = jobs._notes_sync_outcome(_refusal(code))
    assert out["status"] == "skipped"
    assert out["refusal"] == code
    assert out["intent_id"] is None
    if code == "unwired":
        assert "GUI helper" in out["reason"] and "nothing was filed" in out["reason"]
    if code == "offline":
        assert "laptop closed is normal" in out["reason"]


def test_notes_sync_job_never_reports_a_refusal_or_open_intent_as_success():
    from astra.scheduler import jobs

    filed_open = client.IntentResult(intent_id=7, status="claimed", verb="notes.sync")
    assert jobs._notes_sync_outcome(filed_open)["status"] == "skipped"
    filed_denied = client.IntentResult(
        intent_id=7, status="denied", verb="notes.sync",
        deny_reason="verb notes.sync is in the catalogue but not wired yet",
    )
    out = jobs._notes_sync_outcome(filed_denied)
    assert out["status"] == "failed" and out["intent_id"] == 7
    ok = client.IntentResult(intent_id=8, status="succeeded", verb="notes.sync",
                             result_bytes=b"synced 3", receipt_verdict="verified")
    out = jobs._notes_sync_outcome(ok)
    assert out["status"] == "success" and out["receipt"] == "verified"


@pytest.mark.parametrize("code", sorted(client.REFUSAL_CODES))
def test_ingest_reader_gives_a_remedy_that_can_work(monkeypatch, code):
    from astra.tools import reply_tools

    async def _run_intent(verb, args, **kw):
        return _refusal(code, verb=verb)

    monkeypatch.setattr(client, "run_intent", _run_intent)
    out = _run(reply_tools._read_mac_file(_ROOT + "/chat.txt"))
    assert out.error and out.data == b"" and out.pages == 0
    low = out.error.lower()
    if code == "offline":
        assert "closed laptop is normal" in low and "retry when it is awake" in low
    elif code in ("no_body", "ambiguous_body"):
        assert "enrolment" in low and "do not tell kunal to open it" in low
    elif code == "args":
        assert "policy compiled into the mac" in low
        assert not any(p in low for p in _LAPTOP_ADVICE), out.error
    else:
        assert not any(p in low for p in _LAPTOP_ADVICE), out.error


def test_ingest_reader_matches_executor_text_only_on_a_filed_row(monkeypatch):
    from astra.tools import reply_tools

    async def _withheld(verb, args, **kw):
        return client.IntentResult(
            intent_id=9, status="denied", verb=verb,
            deny_reason="result withheld: the file contains an OpenAI/Anthropic-"
                        "style key at byte 38. Secrets do not leave the machine.",
        )

    monkeypatch.setattr(client, "run_intent", _withheld)
    out = _run(reply_tools._read_mac_file(_ROOT + "/chat.txt"))
    assert "byte 38" in out.error and "strip the credential-shaped span" in out.error

    async def _failed(verb, args, **kw):
        return client._from_row(_row(id=11, result_bytes=b"limit 700000 exceeds "
                                     b"the 655360-byte ceiling"))

    monkeypatch.setattr(client, "run_intent", _failed)
    out = _run(reply_tools._read_mac_file(_ROOT + "/chat.txt"))
    assert "intent #11 failed" in out.error and "655360-byte ceiling" in out.error


def test_ingest_reader_never_matches_executor_phrases_on_a_client_refusal(monkeypatch):
    """A client refusal whose words happen to contain an executor
    phrase must still be routed by its code."""
    from astra.tools import reply_tools

    async def _sneaky(verb, args, **kw):
        return client.IntentResult(
            intent_id=None, status="refused", verb=verb, refusal_code="offline",
            deny_reason="body offline; (result withheld is not what this is)",
        )

    monkeypatch.setattr(client, "run_intent", _sneaky)
    out = _run(reply_tools._read_mac_file(_ROOT + "/chat.txt"))
    assert "strip the credential-shaped span" not in out.error
    assert "closed laptop is normal" in out.error.lower()
