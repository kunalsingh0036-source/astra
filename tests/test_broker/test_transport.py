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


def test_submit_intent_is_interactive_only_and_poll_status_is_not():
    """THE surface decision, pinned.

    exec.shell, fs.write and fs.edit are already compiled into the
    shipped broker catalogue. Filing an intent for one raises a Touch ID
    prompt on Kunal's Mac. A prompt-injected WhatsApp turn today cannot
    even name local_bash; if submit_intent were reachable unattended,
    A3 would hand that back through a new door.

    poll_status must NOT be restricted — reading a status causes
    nothing, and an unattended turn should be able to report progress.
    """
    from astra.runtime.tool_surface import (
        interactive_only_tool_names, surface_for_channel,
    )

    io = interactive_only_tool_names()
    assert "submit_intent" in io, (
        "submit_intent is reachable from unattended turns — a WhatsApp "
        "message could raise a Touch ID prompt on Kunal's Mac"
    )
    assert "poll_status" not in io
    assert surface_for_channel("whatsapp") == "unattended"


def test_submit_intent_refuses_when_no_body_is_registered(monkeypatch):
    """A3 ships the pipe; a body enrols later. Until then this must
    refuse with an honest message rather than filing intents nothing
    will ever claim. CHARTER §8: nothing may silently no-op.

    The body lookup is stubbed rather than hitting a database: the repo
    conftest refuses any non-local DB session, because tests writing to
    production has already happened here (40 approval rows carry
    resolution_source='test'). A unit test must not need that guard to
    save it.
    """
    import asyncio
    from astra.runtime.tools import physical

    async def _none():
        return None, (
            "No body is registered, so there is nothing to carry this "
            "out. Tell Kunal — do not retry."
        )

    monkeypatch.setattr(physical, "_only_body", _none)
    out = asyncio.run(physical.submit_intent_impl(
        {"verb": "fs.read", "args": {"path": "/private/tmp"}, "why": "t"}))
    assert out.get("is_error") is True
    text = out["content"][0]["text"]
    assert "No body is registered" in text
    assert "do not retry" in text.lower()


def test_submit_intent_validates_before_touching_the_database(monkeypatch):
    """Bad input must be refused with a readable message, and the
    refusal must not depend on reaching Postgres."""
    import asyncio
    from astra.runtime.tools import physical

    async def _boom():
        raise AssertionError("must not reach the body lookup")

    monkeypatch.setattr(physical, "_only_body", _boom)
    for bad, expect in [
        ({"verb": "", "args": {}, "why": "x"}, "verb is required"),
        ({"verb": "fs.read", "args": "nope", "why": "x"}, "must be an object"),
        ({"verb": "fs.read", "args": {}, "why": ""}, "why is required"),
    ]:
        out = asyncio.run(physical.submit_intent_impl(bad))
        assert out.get("is_error") is True
        assert expect in out["content"][0]["text"]


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


def test_poll_status_never_claims_an_unverifiable_receipt_is_verified():
    """The verdict is RECOMPUTED, and 'cannot check' must never render
    as a pass. There is no receipt_verified column precisely because the
    brain is a superuser and would be writing it about itself."""
    from astra.runtime.tools import physical

    assert physical.EXECUTOR_PUBKEY_HEX == "", "test assumes no key pinned yet"
    v = physical._verify_receipt({"receipt_bytes": b"\x00" * 194})
    assert "UNVERIFIED" in v and "not treat" in v.lower() or "Do not treat" in v

    assert physical._verify_receipt({"receipt_bytes": None}) == "none present"
    assert "MALFORMED" in physical._verify_receipt(
        {"receipt_bytes": b"\x00" * 100})


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
