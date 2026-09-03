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
