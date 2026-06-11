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

Scope: DB-integration test paths are listed in _DB_TEST_PATHS. If you
add a new test dir/file that opens a DB session, add it there — the
guard can't detect DB usage statically.
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


@pytest.fixture
def anyio_backend():
    return "asyncio"
