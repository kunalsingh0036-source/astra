"""Shared test fixtures for Astra test suite.

PRODUCTION-DB GUARD
───────────────────
On 2026-06-11 a full local pytest run executed tests/test_memory/
against the PRODUCTION Railway database, because the developer .env
points DATABASE_URL at prod and the tests build their engine straight
from settings.database_url. The run wiped every WORKING-type memory
(test_clear_working_memory does an unscoped DELETE) and inserted test
fixture strings into Astra's real semantic recall.

The guard below makes that structurally impossible: any test that
touches the database is skipped unless DATABASE_URL points at a local
host. CI is unaffected (its service container is localhost:5432).
To deliberately run DB tests against a remote host — don't. If you
absolutely must, set ASTRA_ALLOW_REMOTE_DB_TESTS=1 and accept that
you are pointing live ammunition at whatever that URL resolves to.

Scope: EVERY test, not a hand-maintained list. The original guard
gated on a _DB_TEST_PATHS allowlist, and on 2026-08-28 that failed
exactly as its own docstring predicted: tests/test_phase_c_approvals.py
and tests/test_autonomy/test_containment_now.py both open real
sessions, neither was listed, and both wrote rows into the production
approvals table. A guard you have to remember to update is not a
guard.

Now the connection itself is blocked: when DATABASE_URL is non-local,
async_session() raises on use for the whole session. A test that
touches the DB fails loudly with an actionable message instead of
silently hitting prod; a test that doesn't touch the DB is unaffected
and still runs. _DB_TEST_PATHS is kept only to SKIP the known
DB-integration suites (which would otherwise fail rather than skip).
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

import pytest

# Hosts considered safe for destructive test runs.
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "host.docker.internal"}

# Test paths that open real DB sessions (directly or transitively).
# Relative to the repo root. Keep this list current — new
# DB-integration suites MUST be added here.
_DB_TEST_PATHS = (
    "tests/test_memory",
    "tests/test_e2e.py",
    # Added 2026-08-28 after this suite was found writing rows into the
    # PRODUCTION approvals table on every local run (ids 88-98). The
    # connection guard below would now fail it loudly; listing it here
    # turns that into a clean skip instead of a permanently red suite.
    "tests/test_phase_c_approvals.py",
)


def _database_host() -> str | None:
    """Best-effort host extraction from the configured DATABASE_URL."""
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        try:
            from astra.config import settings

            url = settings.database_url
        except Exception:
            return None
    # urlparse needs a scheme it recognises; the +asyncpg suffix is fine.
    try:
        return urlparse(url).hostname
    except Exception:
        return None


def pytest_collection_modifyitems(config, items):
    if os.environ.get("ASTRA_ALLOW_REMOTE_DB_TESTS", "") == "1":
        return
    host = _database_host()
    if host is None or host in _LOCAL_HOSTS:
        return
    skip = pytest.mark.skip(
        reason=(
            f"DATABASE_URL points at non-local host {host!r} — refusing "
            "to run DB-integration tests against what may be production. "
            "Point DATABASE_URL at a local Postgres, or (dangerous) set "
            "ASTRA_ALLOW_REMOTE_DB_TESTS=1."
        )
    )
    for item in items:
        path = str(item.fspath)
        if any(p in path for p in _DB_TEST_PATHS):
            item.add_marker(skip)


@pytest.fixture(scope="session", autouse=True)
def _block_remote_db_connections():
    """Structural backstop: make a remote DATABASE_URL un-connectable
    for the whole test session.

    The path-allowlist skip above only covers suites someone
    remembered to list. This covers every test that ever opens a
    session — including ones written next year — by replacing the
    sessionmaker with one that raises. The error names the fix.

    Deliberately NOT active when ASTRA_ALLOW_REMOTE_DB_TESTS=1 (the
    documented, dangerous escape hatch) or when the host is local.
    """
    if os.environ.get("ASTRA_ALLOW_REMOTE_DB_TESTS", "") == "1":
        yield
        return
    host = _database_host()
    if host is None or host in _LOCAL_HOSTS:
        yield
        return

    import astra.db.engine as _engine

    def _refuse(*_args, **_kwargs):
        raise RuntimeError(
            f"DB session refused: DATABASE_URL points at {host!r}, "
            "which is not a local host. This test opened a real "
            "database session and would have written to what may be "
            "production (this has happened: prod approvals rows on "
            "2026-08-28). Point DATABASE_URL at a local Postgres, "
            "mock the session, or — knowing the risk — set "
            "ASTRA_ALLOW_REMOTE_DB_TESTS=1."
        )

    original = _engine.async_session
    _engine.async_session = _refuse
    try:
        yield
    finally:
        _engine.async_session = original


@pytest.fixture
def anyio_backend():
    return "asyncio"
