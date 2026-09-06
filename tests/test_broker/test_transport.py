"""The A3 transport: what must be true of the cloud half.

These are the checks that need no database. The integration gate — every
SQL string executed against production inside a rolled-back transaction
— is the dry-run script, run by hand before each deploy. That split is
deliberate: a test suite that writes to production is how 40 rows in the
live `approvals` table came to carry resolution_source='test'.
"""

from __future__ import annotations

import inspect

import pytest

from astra.broker import store


# ── the ownership predicate ───────────────────────────────


def test_record_status_requires_body_id_as_a_keyword():
    """UNCONDITIONAL, not optional.

    finalize_call takes `bridge_token_id: int | None = None` — its own
    docstring admits that is backward-compatibility debt. Optional means
    the first forgetful caller silently reopens cross-body result
    forgery, and with one body registered nothing ever fails visibly.
    So here it is a required keyword: forgetting it is a TypeError at
    the call site, not a missing predicate at the database.
    """
    sig = inspect.signature(store.record_status)
    p = sig.parameters["body_id"]
    assert p.kind is inspect.Parameter.KEYWORD_ONLY, (
        "body_id must be keyword-only so it cannot be passed positionally "
        "by accident"
    )
    assert p.default is inspect.Parameter.empty, (
        "body_id must have NO DEFAULT. An optional ownership predicate is "
        "one forgotten argument away from being no predicate."
    )


def test_record_status_sql_constrains_body_and_open_status():
    src = inspect.getsource(store.record_status)
    where = src[src.find("WHERE"):]
    assert "body_id = :body_id" in where, (
        "the UPDATE lost its ownership predicate"
    )
    assert "status IN" in where and "'claimed'" in where, (
        "a terminal intent must be unwritable — without this guard a "
        "replayed status POST rewrites a finished result"
    )
    assert '"body_id"' in src, "body_id is referenced but never bound"


def test_claim_is_a_single_statement_with_skip_locked():
    src = inspect.getsource(store.claim_next_intent)
    assert "FOR UPDATE SKIP LOCKED" in src, (
        "two bodies would claim the same intent"
    )
    assert src.count("UPDATE intents") == 1, (
        "the claim must be ONE statement; SELECT-then-UPDATE races"
    )
    assert "expires_at > now()" in src, (
        "an expired intent must never be handed out, reaper or not"
    )


def test_audit_append_cannot_overwrite_a_sequence_number():
    src = inspect.getsource(store.append_audit)
    assert "ON CONFLICT (body_id, seq) DO NOTHING" in src, (
        "a replayed seq must not overwrite the chain"
    )
    assert "RETURNING id" in src, (
        "without RETURNING, a no-op insert is indistinguishable from a "
        "successful one"
    )


# ── no authority anywhere in the cloud half ───────────────


def test_store_never_writes_an_authority_column():
    """The schema has no such column; this catches someone adding one."""
    src = inspect.getsource(store)
    for banned in ("approved", "approver", "approval_pubkey",
                   "permission_epoch", "receipt_verified"):
        # Allowed in prose, never in SQL.
        sql_ish = [ln for ln in src.splitlines()
                   if banned in ln and any(
                       kw in ln.upper()
                       for kw in ("INSERT", "UPDATE", "SELECT", "SET ", "VALUES"))]
        assert not sql_ish, (
            f"{banned} appears in SQL: {sql_ish[:1]}. The brain is a "
            "Postgres superuser, so a column it can set is a column it "
            "can set on itself."
        )


def test_get_intent_status_returns_raw_bytes_not_a_verdict():
    src = inspect.getsource(store.get_intent_status)
    assert "receipt_bytes" in src and "display_bytes" in src
    assert "verified" not in src.lower().split("Never add")[0].split('"""')[-1], (
        "poll_status must RECOMPUTE verification from the executor's "
        "public key, never read a boolean the superuser can write"
    )


# ── unstorable arguments are refused, not 500s ────────────


@pytest.mark.parametrize("args,where", [
    ({"p": "a\x00b"}, "args.p"),
    ({"nested": {"deep": "x\x00y"}}, "args.nested.deep"),
    ({"list": ["ok", "bad\x00"]}, "args.list[1]"),
])
def test_nul_in_args_is_refused_with_a_readable_message(args, where):
    """Postgres JSONB cannot store U+0000 — asyncpg raises
    UntranslatableCharacterError. Verified against production. Without
    this check it is a 500 on the first real request, which is exactly
    how the bridge result path broke earlier today."""
    with pytest.raises(store.ArgsRejected) as e:
        store._reject_unstorable(args)
    assert where in str(e.value)
    assert "nothing was filed" in str(e.value) or "NUL" in str(e.value)


def test_ordinary_args_pass():
    store._reject_unstorable({"path": "/private/tmp", "limit": 10,
                              "nested": {"a": ["b", "c"]}})


def test_token_is_hashed_never_stored_plaintext():
    src = inspect.getsource(store)
    assert "sha256" in src
    assert "token_hash" in src
    mint = inspect.getsource(store.mint_body_token)
    assert "_hash_token(tok)" in mint, (
        "the plaintext token must never reach the database"
    )
    assert "secrets.token_urlsafe" in src


# ── the test guard itself ─────────────────────────────────


def test_conftest_refuses_a_non_loopback_dsn():
    """The fixture is the only way to get a DSN, and it cannot return a
    remote one. This asserts the guard exists and names loopback."""
    import pathlib
    src = (pathlib.Path(__file__).parent / "conftest.py").read_text()
    assert "_LOOPBACK" in src
    assert "pytest.fail" in src, (
        "a non-loopback DSN must FAIL the suite, not skip it quietly"
    )


# ── the three wiring points, each a silent-failure trap ───


def test_both_tools_are_actually_registered():
    """physical.py can be perfect and register NOTHING without the one
    import line in astra/runtime/tools/__init__.py. That is exactly how
    notes_sync sat unregistered for 40 days while its body was correct.
    A missing tool is silent: the model simply never calls it."""
    import astra.runtime.tools  # noqa: F401
    from astra.runtime.tool_registry import REGISTRY

    for name in ("submit_intent", "poll_status"):
        assert REGISTRY.get(name) is not None, (
            f"{name} is not registered — check the import line in "
            "astra/runtime/tools/__init__.py"
        )


def test_tools_are_classified_in_tool_tiers():
    """An unclassified tool falls back to DESTRUCTIVE at
    modes.py:326 — safe, but it means the gate asks about a tool that
    causes nothing, forever.

    Compared BY VALUE, not identity: this repo has TWO ActionTier
    enums — astra.autonomy.modes defines its own (a str-Enum, used by
    TOOL_TIERS and PERMISSION_MATRIX) and astra.runtime.tool_registry
    defines another (a plain Enum, used at registration). Same members,
    different classes, so `is` fails across them. Production never
    crosses the boundary — the gate resolves tiers through
    modes.ActionTier end to end — but a test that used `is` here would
    fail for a reason that has nothing to do with what it is checking.
    """
    from astra.autonomy.modes import ActionTier as ModesTier, TOOL_TIERS

    assert TOOL_TIERS.get("submit_intent") == ModesTier.WRITE
    assert TOOL_TIERS.get("poll_status") == ModesTier.READ


def test_the_two_actiontier_enums_have_not_diverged():
    """Guard on the dual-enum arrangement itself.

    Two enums with the same name in one codebase is a trap. It is
    currently harmless because the gate uses only the modes one, but if
    the registry ever gains a tier the modes enum lacks, every tool
    carrying it silently falls back to DESTRUCTIVE — which fails closed,
    yet presents as "the gate keeps asking about a read".
    """
    from astra.autonomy.modes import ActionTier as ModesTier
    from astra.runtime.tool_registry import ActionTier as RegistryTier

    assert {m.value for m in ModesTier} == {m.value for m in RegistryTier}, (
        "the two ActionTier enums have diverged; a tool registered with a "
        "tier the autonomy module does not know falls back to DESTRUCTIVE"
    )


def test_submit_intent_is_reachable_from_whatsapp():
    """Kunal's call, and the evidence supports it.

    The first version made submit_intent interactive-only, reasoning
    that a prompt-injected WhatsApp message could raise Touch ID prompts
    on his Mac. That threat does not exist through this path:
    services/gateway/api/webhook.py:300 gates the chat path on
    is_owner(phone), so only ASTRA_OWNER_NUMBERS can start a WhatsApp
    turn, and no scheduler starts turns at all.

    The fingerprint is the gate and the display is bound to what
    executes — neither depends on which transport carried the request.
    Blocking it cost the ability to ask for something on the Mac from
    the phone, which is exactly when a body is most useful.
    """
    from astra.runtime.tool_surface import (
        allowed_tool_names, interactive_only_tool_names,
    )

    import astra.runtime.tools  # noqa: F401
    from astra.runtime.tool_registry import REGISTRY

    io = interactive_only_tool_names()
    assert "submit_intent" not in io
    assert "poll_status" not in io
    assert "body_status" not in io
    allowed, _ = allowed_tool_names(
        ["submit_intent", "poll_status", "body_status", "edit_astra_file"],
        "unattended")
    assert {"submit_intent", "poll_status", "body_status"} <= set(allowed)
    # The unstructured Mac shell tool used to be the negative case here
    # ("stays blocked on unattended"). Since A6 it is not a surface
    # question: the name is absent from the registry on every surface
    # and refused at boot (tool_registry._FORBIDDEN).
    assert "local_bash" not in REGISTRY.names()


def test_absent_or_unknown_channel_is_least_privilege():
    """An absent channel used to mean 'web' and therefore the FULL
    Mac-writing surface, so any caller that forgot the field silently
    received maximum privilege — least privilege exactly backwards, and
    silent because forgetting a field looks like nothing.

    Only an explicit "web" is interactive now. astra-web sends it.
    """
    from astra.runtime.tool_surface import surface_for_channel

    assert surface_for_channel("web") == "interactive"
    assert surface_for_channel(None) == "unattended"
    assert surface_for_channel("") == "unattended"
    assert surface_for_channel("whatsapp") == "unattended"
    assert surface_for_channel("some-new-caller") == "unattended"


def test_the_web_app_sends_its_channel_explicitly():
    """Load-bearing after the default flipped: if astra-web stops
    sending channel:"web", the web app quietly loses its Mac tools."""
    import pathlib as _p

    route = (
        _p.Path(__file__).resolve().parents[3]
        / "astra-web/app/api/chat/route.ts"
    )
    if not route.exists():
        pytest.skip("astra-web not checked out beside astra")
    assert 'channel: "web"' in route.read_text(), (
        "astra-web no longer sends an explicit channel — with the "
        "least-privilege default it will fall to the unattended surface"
    )


def test_submit_intent_refuses_when_no_body_is_registered(monkeypatch):
    """A3 ships the pipe; a body enrols later. Until then this must
    refuse with an honest message rather than filing intents nothing
    will ever claim. CHARTER §8: nothing may silently no-op.

    The body lookup is stubbed rather than hitting a database: the repo
    conftest refuses any non-local DB session, because tests writing to
    production has already happened here (40 approval rows carry
    resolution_source='test'). A unit test must not need that guard to
    save it. Since A6 the lookup lives in astra.broker.client, the one
    chokepoint the model's tool and every code caller share.
    """
    import asyncio
    from astra.broker import client
    from astra.runtime.tools import physical

    async def _none():
        return None, (
            "No body is registered, so there is nothing to carry this "
            "out. Tell Kunal — do not retry."
        )

    monkeypatch.setattr(client, "_only_body", _none)
    out = asyncio.run(physical.submit_intent_impl(
        {"verb": "fs.read", "args": {"path": "/private/tmp"}, "why": "t"}))
    assert out.get("is_error") is True
    text = out["content"][0]["text"]
    assert "No body is registered" in text
    assert "do not retry" in text.lower()


def test_submit_intent_validates_before_touching_the_database(monkeypatch):
    """Bad input must be refused with a readable message, and the
    refusal must not depend on reaching Postgres: neither the body
    lookup nor the insert may be reached."""
    import asyncio
    from astra.broker import client, store
    from astra.runtime.tools import physical

    async def _boom(*a, **k):
        raise AssertionError("must not reach the database")

    monkeypatch.setattr(client, "_only_body", _boom)
    monkeypatch.setattr(store, "submit_intent", _boom)
    for bad, expect in [
        ({"verb": "", "args": {}, "why": "x"}, "verb is required"),
        ({"verb": "fs.read", "args": "nope", "why": "x"}, "must be an object"),
        ({"verb": "fs.read", "args": {}, "why": ""}, "why is required"),
        # the client's own pre-validation, named path and all
        ({"verb": "fs.read", "args": {}, "why": "x"}, "requires ['path']"),
        ({"verb": "fs.read", "args": {"path": "/x", "bogus": 1}, "why": "x"},
         "not an argument of fs.read"),
        ({"verb": "fs.read", "args": {"path": "/x", "limit": 1.5}, "why": "x"},
         "float"),
        ({"verb": "fs.read", "args": {"path": "/x", "reason": "no"}, "why": "x"},
         "forbidden key"),
        ({"verb": "nope.verb", "args": {}, "why": "x"}, "not a catalogue verb"),
    ]:
        out = asyncio.run(physical.submit_intent_impl(bad))
        assert out.get("is_error") is True, bad
        assert expect in out["content"][0]["text"], (bad, out)


def test_nul_in_args_is_refused_before_the_insert():
    """A U+0000 anywhere in the document makes asyncpg raise
    UntranslatableCharacterError — verified against production. Without
    this check it is a 500 on the first real request, which is exactly
    the failure that took the bridge result path down."""
    import pytest as _pytest
    from astra.broker.store import ArgsRejected, _reject_unstorable

    _reject_unstorable({"path": "/private/tmp", "n": 1})          # fine
    for bad in (
        {"path": "/tmp/\x00evil"},
        {"nested": {"k": "a\x00b"}},
        {"list": ["ok", "b\x00d"]},
    ):
        with _pytest.raises(ArgsRejected, match="NUL"):
            _reject_unstorable(bad)


# ── the intent client: refusals before any row, and the claim ──


async def _db_must_not_be_reached(*a, **k):
    raise AssertionError("the database must not be reached for this refusal")


def test_run_intent_refuses_a_signed_verb_outside_a_turn(monkeypatch):
    """The habituation control: outside a turn (scheduler jobs) only
    auto verbs may be filed, so no job can ever raise a Touch ID
    prompt. Refused before the body lookup; nothing filed. A control,
    not a gate: the fingerprint on the Mac is the gate."""
    import asyncio
    from astra.broker import client, store
    from astra.autonomy.turn_context import current_turn

    monkeypatch.setattr(client, "_only_body", _db_must_not_be_reached)
    monkeypatch.setattr(store, "submit_intent", _db_must_not_be_reached)
    assert current_turn.get() == ""
    r = asyncio.run(client.run_intent(
        "fs.write", {"path": "/private/tmp/x", "content": "y"},
        why="t", actor="some_job"))
    assert r.status == "refused" and not r.filed
    assert "not inside a turn" in r.deny_reason
    assert "Kunal was not asked" in r.deny_reason


def test_run_intent_refuses_an_unwired_verb_before_the_body_lookup(monkeypatch):
    """Intent #10 class: a real Touch ID tap, then 'not wired yet'. An
    unwired verb is refused before filing so nobody is ever asked for
    an action the executor will refuse. The mirror that says which
    verbs are wired is pinned to the Swift sources by
    test_catalogue_mirror.py."""
    import asyncio
    from astra.broker import client, store

    monkeypatch.setattr(client, "_only_body", _db_must_not_be_reached)
    monkeypatch.setattr(store, "submit_intent", _db_must_not_be_reached)
    unwired_auto = sorted(client.AUTO_VERBS - client.WIRED_VERBS)
    if not unwired_auto:
        pytest.skip("every auto verb is wired in this build")
    verb = unwired_auto[0]
    spec = client.CATALOGUE_BY_NAME[verb]
    args = {k: "/private/tmp/x" for k in spec.required_keys}
    r = asyncio.run(client.run_intent(verb, args, why="t", actor="chat"))
    assert r.status == "refused" and not r.filed
    assert "cannot perform" in r.deny_reason and verb in r.deny_reason
    assert "Kunal was not asked" in r.deny_reason


def test_run_intent_actor_is_keyword_only_and_required():
    """`actor` names the code path filing the intent. It is keyword-
    only and never a tool argument: the model composes every tool
    argument, so a fact about who is calling can never come from the
    args (the property `on_behalf_of` had at the retired chokepoint)."""
    import inspect
    from astra.broker import client

    p = inspect.signature(client.run_intent).parameters["actor"]
    assert p.kind is inspect.Parameter.KEYWORD_ONLY
    assert p.default is inspect.Parameter.empty
    tool_args = client.CATALOGUE_BY_NAME["fs.read"].arg_keys
    assert "actor" not in tool_args


def _wired_auto_verb():
    from astra.broker import client

    for v in client.CATALOGUE:
        if v.wired and not v.signed and v.required_keys:
            return v
    pytest.skip("no wired auto verb with a required key in this build")


def test_run_intent_session_claim_is_turn_inside_and_job_outside(monkeypatch):
    """The claim written to intents.session_claim: `turn:<id>` inside
    a turn, `job:<actor>` outside. A claim, asserted by the cloud about
    itself; the broker_notify job and the resolved-intents block key
    on the prefix, so its shape is load-bearing."""
    import asyncio
    from astra.broker import client, store
    from astra.autonomy.turn_context import current_turn

    captured: list[dict] = []

    async def _one_body():
        return 1, ""

    async def _live(body_id):
        return store.BodyLiveness(
            body_id=1, label="test", last_poll_at=None,
            last_completed_at=None, poll_age_sec=5.0, revoked=False,
        )

    async def _depth(body_id):
        return 0

    async def _submit(**kw):
        captured.append(kw)
        return 77

    monkeypatch.setattr(client, "_only_body", _one_body)
    monkeypatch.setattr(store, "body_liveness", _live)
    monkeypatch.setattr(store, "pending_depth", _depth)
    monkeypatch.setattr(store, "submit_intent", _submit)

    v = _wired_auto_verb()
    args = {k: "/private/tmp/x" for k in v.required_keys}

    r = asyncio.run(client.run_intent(v.name, args, why="t", actor="notes_sync"))
    assert r.filed and r.intent_id == 77 and r.status == "pending"
    assert captured[-1]["session_claim"] == "job:notes_sync"
    assert captured[-1]["body_id"] == 1

    async def _in_turn():
        tok = current_turn.set("turn:42")
        try:
            return await client.run_intent(v.name, args, why="t", actor="chat")
        finally:
            current_turn.reset(tok)

    r = asyncio.run(_in_turn())
    assert r.filed
    assert captured[-1]["session_claim"] == "turn:42"


def test_run_intent_refuses_a_body_that_is_not_polling(monkeypatch):
    """A closed laptop is normal and is refused in one round trip,
    never a 300 s wait on a row nothing will claim. The message
    carries Kunal's standing rule (offer retry or a task tagged
    'body'; never volunteer 'the Mac is offline')."""
    import asyncio
    from astra.broker import client, store

    async def _one_body():
        return 1, ""

    async def _stale(body_id):
        return store.BodyLiveness(
            body_id=1, label="test", last_poll_at=None,
            last_completed_at=None,
            poll_age_sec=client.BODY_POLL_WINDOW_SEC + 1, revoked=False,
        )

    monkeypatch.setattr(client, "_only_body", _one_body)
    monkeypatch.setattr(store, "body_liveness", _stale)
    monkeypatch.setattr(store, "submit_intent", _db_must_not_be_reached)
    v = _wired_auto_verb()
    args = {k: "/private/tmp/x" for k in v.required_keys}
    r = asyncio.run(client.run_intent(v.name, args, why="t", actor="chat"))
    assert r.status == "refused" and not r.filed
    assert "body offline" in r.deny_reason
    assert "'body'" in r.deny_reason and "Do not volunteer" in r.deny_reason


def test_wait_for_intent_only_reads():
    """The watcher never writes: the retired bridge's wait_for_result
    marked rows 'timeout' on the courier's own patience, which made
    the cloud's deadline part of the row's truth. Here None means 'not
    finished as far as this caller waited', nothing more."""
    import inspect

    src = inspect.getsource(store.wait_for_intent)
    for kw in ("UPDATE", "INSERT", "DELETE"):
        assert kw not in src.upper().replace("UPDATED", ""), (
            f"wait_for_intent contains {kw}: the watcher must not write"
        )
    assert "get_intent_status" in src


# ── receipts: recomputed, never stored ────────────────────


def test_poll_status_never_claims_an_unverifiable_receipt_is_verified(monkeypatch):
    """The verdict is RECOMPUTED, and 'cannot check' must never render
    as a pass. There is no receipt_verified column precisely because the
    brain is a superuser and would be writing it about itself. Since A6
    the verifier is astra.broker.client.verify_receipt and the key is
    settings.executor_pubkey_hex (env EXECUTOR_PUBKEY_HEX)."""
    from astra.broker import client

    monkeypatch.setattr(client.settings, "executor_pubkey_hex", "")
    v = client.verify_receipt({"receipt_bytes": b"\x00" * 194})
    assert "UNVERIFIED" in v
    assert "not treat" in v.lower()

    assert client.verify_receipt({"receipt_bytes": None}) == "none present"
    assert "MALFORMED" in client.verify_receipt({"receipt_bytes": b"\x00" * 100})


def _signed_receipt(priv, result: bytes) -> bytes:
    """Receipt.swift layout: [0:130] payload, of which [94:126] is
    SHA-256 of the result bytes; [130:194] the Ed25519 signature."""
    import hashlib

    payload = bytearray(b"\x01" * 130)
    payload[94:126] = hashlib.sha256(result).digest()
    payload = bytes(payload)
    return payload + priv.sign(payload)


def test_receipt_verifies_only_with_the_pinned_key_and_matching_result(monkeypatch):
    """The positive half: with EXECUTOR_PUBKEY_HEX pinned, a receipt
    signed by that key over the result's digest is 'verified'; a
    tampered result or a foreign key is named as forged, never as a
    pass or as 'unverified'."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )
    from cryptography.hazmat.primitives import serialization

    from astra.broker import client

    priv = Ed25519PrivateKey.generate()
    pub_hex = priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw,
    ).hex()
    monkeypatch.setattr(client.settings, "executor_pubkey_hex", pub_hex)

    result = b"hello from the executor"
    rb = _signed_receipt(priv, result)
    assert len(rb) == 194
    assert client.verify_receipt(
        {"receipt_bytes": rb, "result_bytes": result}) == "verified"

    tampered = client.verify_receipt(
        {"receipt_bytes": rb, "result_bytes": result + b"!"})
    assert "DOES NOT MATCH" in tampered and "forged" in tampered

    other = Ed25519PrivateKey.generate()
    foreign = client.verify_receipt(
        {"receipt_bytes": _signed_receipt(other, result),
         "result_bytes": result})
    assert "DID NOT VERIFY" in foreign and "forged" in foreign

    # And the status path uses the same verifier for a succeeded row.
    r = client._from_row({
        "id": 1, "status": "succeeded", "verb": "fs.read",
        "receipt_bytes": rb, "result_bytes": result,
    })
    assert r.receipt_verdict == "verified"


def test_submit_intent_description_tells_the_model_not_to_poll_in_a_loop():
    """A signed verb waits on a human who may be nowhere near the Mac,
    against a 240s turn cap and 25 tool iterations. Without an explicit
    instruction the model burns the whole turn polling."""
    from astra.runtime.tool_registry import REGISTRY
    import astra.runtime.tools  # noqa: F401

    d = REGISTRY.get("submit_intent").description.lower()
    assert "once" in d and ("do not poll in a loop" in d or "stop" in d)
    p = REGISTRY.get("poll_status").description.lower()
    assert "once" in p and "stop" in p
