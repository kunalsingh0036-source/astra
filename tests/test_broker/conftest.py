"""Test-side guard rail for the broker suite.

THE PROBLEM THIS SOLVES. A3's acceptance test is, by design, a scripted
superuser attack: it writes intents, forges receipts, deletes audit
events. That artifact is meant to be RUN, repeatedly, by anyone. Point
it at production once and it does exactly what it says on the tin.

This repo already has that failure: 40 rows in the production
`approvals` table carry resolution_source='test'.

So the DSN is not a variable, it is a fixture, and the fixture is the
only way to get one. It refuses any host that is not loopback. A
reviewer cannot forget to check, because there is nothing to check —
a test that wants a database must ask for `local_dsn`, and `local_dsn`
cannot return a remote one.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

import pytest

_LOOPBACK = {"localhost", "127.0.0.1", "::1", ""}


@pytest.fixture(scope="session")
def local_dsn() -> str:
    """A LOOPBACK-ONLY Postgres DSN, or skip.

    Set ASTRA_TEST_DSN to a local database to run the integration
    tests. Anything not on loopback fails the suite loudly rather than
    being quietly used.
    """
    dsn = (os.environ.get("ASTRA_TEST_DSN") or "").strip()
    if not dsn:
        pytest.skip(
            "ASTRA_TEST_DSN is not set. The broker integration tests "
            "need a LOCAL Postgres; they must never run against "
            "production. The production pre-deploy gate is the dry-run "
            "script, which rolls back — see "
            "astra-body/docs/WORKSTREAM-A-DESIGN.md."
        )
    host = (urlparse(dsn).hostname or "").lower()
    if host not in _LOOPBACK:
        pytest.fail(
            f"ASTRA_TEST_DSN points at {host!r}, which is not loopback. "
            "These tests write forged intents and delete audit rows. "
            "Refusing.",
            pytrace=False,
        )
    return dsn
