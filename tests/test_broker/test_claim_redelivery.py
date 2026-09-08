"""Delivery must survive a lost response.

Intent #57 — the first real fs.write — was marked `claimed` by the
cloud, its hand-over was lost on a link that times out several times an
hour, and nothing ever offered it again. It sat claimed until it
expired while the broker sat idle and logged nothing. `claim_next_intent`
only ever selected `status = 'pending'`, so a claim recorded before the
broker had the bytes was a claim recorded forever.
"""
import re
from pathlib import Path

SRC = (Path(__file__).resolve().parents[2] / "astra" / "broker" / "store.py").read_text()
QUERY = SRC[SRC.index("async def claim_next_intent"):SRC.index("async def", SRC.index("async def claim_next_intent") + 10)]


def _sql_only(q: str) -> str:
    """The SQL with comments stripped. A guard that reads a comment is
    not a guard — this repo has fired one on a comment before."""
    return re.sub(r"--[^\n]*", "", q)


def test_a_stale_claim_is_offered_again():
    sql = _sql_only(QUERY)
    assert "status = 'claimed'" in sql and "claimed_at <" in sql, (
        "claim_next_intent must offer a stale claim again; without it a "
        "hand-over lost in transit orphans the intent forever"
    )
    assert "make_interval(secs => :stale_secs)" in sql, "the window must be a bound parameter"


def test_a_fresh_claim_is_not_raced():
    """The window must sit above the broker's own request timeout, or a
    slow-but-alive broker gets its work handed to a second delivery."""
    from astra.broker import store

    # It must exceed the longest legitimate time in 'claimed': the
    # executor's 60s watchdog for an irreversible verb plus the broker's
    # 15s HTTP timeout. Below that, a redelivery races a running action
    # and the same intent executes twice.
    assert store.CLAIM_STALE_SECS >= 60 + 15 + 60, store.CLAIM_STALE_SECS


def test_expiry_still_gates_delivery():
    sql = _sql_only(QUERY)
    assert "expires_at > now()" in sql, (
        "an expired intent must never be handed out, redelivery or not"
    )


def test_only_claimed_is_redelivered_never_a_decided_intent():
    """running / awaiting_human / any terminal state means the broker
    DID receive it and made a decision. Redelivering those would run an
    approved action twice."""
    sql = _sql_only(QUERY)
    for decided in ("running", "awaiting_human", "succeeded", "denied", "failed", "expired"):
        assert f"status = '{decided}'" not in sql, (
            f"'{decided}' must never be redelivered: the broker already acted on it"
        )


def test_a_redelivery_is_never_silent():
    body = SRC[SRC.index("async def claim_next_intent"):]
    assert "redelivered" in body and "logger.warning" in body, (
        "a redelivery is evidence the link dropped a hand-over and must be "
        "reported, not absorbed"
    )


def test_the_prior_status_is_read_before_the_update_not_after():
    """RETURNING sees the NEW row, so a naive `status = 'claimed'` in the
    RETURNING clause is always true and reports every delivery as a
    redelivery. The prior status has to come from the CTE."""
    sql = _sql_only(QUERY)
    assert "prior_status" in sql and "WITH pick AS" in sql
    assert sql.index("prior_status") < sql.index("UPDATE intents")
