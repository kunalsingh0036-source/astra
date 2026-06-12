"""Phase C locks — approval store round-trip, grants, expiry.

DB-integration tests: the prod-DB conftest guard applies (they only
run against a local/CI Postgres).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _fresh_engine_pool():
    """The module-global async engine pools connections bound to the
    event loop that created them; pytest-asyncio gives every test its
    own loop. Dispose BEFORE each test (earlier modules in a full run
    leave loop-bound connections in the pool) and AFTER (so we don't
    poison whoever runs next) — without both, tests die with
    'attached to a different loop' depending on suite order (same
    disease as the old test_database_connectivity flake)."""
    from astra.db.engine import engine

    await engine.dispose()
    yield
    await engine.dispose()


async def test_approval_round_trip_one_shot():
    """pending → approved → consumed by exactly ONE grant check."""
    from astra.autonomy.approvals import (
        check_grant,
        create_approval,
        resolve_approval,
    )

    aid = await create_approval(
        tool_name="phase_c_test_tool",
        tool_input={"x": 1},
        reason="test",
        turn_id=None,
        session_id=None,
    )
    # Not granted while pending
    granted, _ = await check_grant("phase_c_test_tool")
    assert granted is False

    out = await resolve_approval(aid, "approved", source="test")
    assert out["ok"] is True

    granted, reason = await check_grant("phase_c_test_tool")
    assert granted is True and "one-shot" in reason
    # Consumed — second check must NOT pass
    granted2, _ = await check_grant("phase_c_test_tool")
    assert granted2 is False


async def test_denied_approval_grants_nothing():
    from astra.autonomy.approvals import (
        check_grant,
        create_approval,
        resolve_approval,
    )

    aid = await create_approval(
        tool_name="phase_c_denied_tool",
        tool_input={},
        reason="test",
        turn_id=None,
        session_id=None,
    )
    await resolve_approval(aid, "denied", source="test")
    granted, _ = await check_grant("phase_c_denied_tool")
    assert granted is False


async def test_standing_grant_persists_and_revokes():
    from astra.autonomy.approvals import (
        check_grant,
        create_approval,
        resolve_approval,
        revoke_grant,
    )

    aid = await create_approval(
        tool_name="phase_c_standing_tool",
        tool_input={},
        reason="test",
        turn_id=None,
        session_id=None,
    )
    await resolve_approval(aid, "approved", standing=True, source="test")
    # Standing: multiple checks all pass
    for _ in range(3):
        granted, reason = await check_grant("phase_c_standing_tool")
        assert granted is True and "standing" in reason
    # Revoke = back to asking
    assert await revoke_grant("phase_c_standing_tool") is True
    granted, _ = await check_grant("phase_c_standing_tool")
    assert granted is False


async def test_resolve_nonexistent_or_double():
    from astra.autonomy.approvals import create_approval, resolve_approval

    out = await resolve_approval(99_999_999, "approved", source="test")
    assert out["ok"] is False

    aid = await create_approval(
        tool_name="phase_c_double_tool",
        tool_input={},
        reason="t",
        turn_id=None,
        session_id=None,
    )
    assert (await resolve_approval(aid, "approved", source="test"))["ok"]
    # Already resolved — second resolve fails cleanly
    assert not (await resolve_approval(aid, "denied", source="test"))["ok"]


async def test_expire_stale_only_touches_old_pending():
    from astra.autonomy.approvals import create_approval, expire_stale, list_pending

    aid = await create_approval(
        tool_name="phase_c_fresh_tool",
        tool_input={},
        reason="t",
        turn_id=None,
        session_id=None,
    )
    expired = await expire_stale(hours=24)
    pending_ids = [r["id"] for r in await list_pending()]
    assert aid in pending_ids, "fresh pending row must survive expiry sweep"
