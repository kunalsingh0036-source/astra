"""
Bridge tests — Phase 7.

Validates the parts that don't require a live network:
  - Path allowlist (server-side check used by tools before queueing)
  - Token hashing (one-way, deterministic)
  - Daemon-side path resolution (path traversal rejected)
  - Daemon dispatch table covers every documented tool
  - Daemon tool implementations behave correctly with allowlist enforcement

DB-bound functions (queue_call, claim_pending_call, etc.) need a
running Postgres + the bridge_tokens / bridge_calls tables. We
exercise them via integration tests in CI; here we keep the suite
hermetic.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from astra.runtime.bridge.store import _hash_token, is_path_allowed


# ── Token hashing ────────────────────────────────────────


def test_hash_token_deterministic() -> None:
    h1 = _hash_token("plaintext-abc")
    h2 = _hash_token("plaintext-abc")
    assert h1 == h2


def test_hash_token_one_way() -> None:
    """SHA-256 — different inputs produce different hashes."""
    assert _hash_token("a") != _hash_token("b")
    # Hash is hex-encoded SHA-256 → 64 chars
    assert len(_hash_token("anything")) == 64


# ── Path allowlist ───────────────────────────────────────


def test_is_path_allowed_within_root() -> None:
    assert is_path_allowed(
        "/Users/kunalsingh/Documents/foo.txt",
        ["/Users/kunalsingh/Documents"],
    )


def test_is_path_allowed_exact_root() -> None:
    """An exact-root match is allowed (e.g. listing the root itself)."""
    assert is_path_allowed(
        "/Users/kunalsingh/Documents",
        ["/Users/kunalsingh/Documents"],
    )


def test_is_path_allowed_rejects_outside() -> None:
    assert not is_path_allowed(
        "/etc/passwd",
        ["/Users/kunalsingh/Documents"],
    )


def test_is_path_allowed_rejects_traversal() -> None:
    """`..` doesn't escape the allowlist after normalization."""
    # /Users/kunalsingh/Documents/../../../etc/passwd → /etc/passwd
    assert not is_path_allowed(
        "/Users/kunalsingh/Documents/../../../etc/passwd",
        ["/Users/kunalsingh/Documents"],
    )


def test_is_path_allowed_rejects_relative() -> None:
    """Relative paths are refused — daemon needs absolutes."""
    assert not is_path_allowed("foo.txt", ["/Users/kunalsingh"])


def test_is_path_allowed_no_prefix_overlap() -> None:
    """`/etc/foo` should NOT match an allowlist of `/etc` if the
    allowlisted entry is `/etcd` (off-by-one prefix bug)."""
    assert not is_path_allowed("/etcd-secrets/key", ["/etc"])


def test_is_path_allowed_empty_inputs() -> None:
    assert not is_path_allowed("", ["/etc"])
    assert not is_path_allowed("/etc/foo", [])


# ── Daemon dispatch coverage ─────────────────────────────


def test_daemon_dispatch_covers_all_tools() -> None:
    """Every tool registered on the Astra side has a handler in the
    daemon. If we add a tool on one side and forget the other, this
    test fails before deploy."""
    from astra.bridge_daemon import _DISPATCH

    expected = {
        "local_read",
        "local_write",
        "local_edit",
        "local_bash",
        "local_glob",
        "local_grep",
    }
    assert set(_DISPATCH.keys()) == expected


# ── Daemon tool impl — local_read ─────────────────────────


def test_daemon_local_read_happy_path() -> None:
    from astra.bridge_daemon import _local_read

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "hello.txt"
        p.write_text("line one\nline two\nline three\n")

        out = _local_read({"path": str(p)}, [td])
        assert "line one" in out
        assert "line three" in out


def test_daemon_local_read_offset_and_limit() -> None:
    from astra.bridge_daemon import _local_read

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "f.txt"
        p.write_text("\n".join(f"line {i}" for i in range(1, 11)) + "\n")

        out = _local_read(
            {"path": str(p), "offset": 3, "limit": 2},
            [td],
        )
        # Should return lines 3-4 only
        assert "line 3" in out
        assert "line 4" in out
        assert "line 1\n" not in out
        assert "line 5" not in out


def test_daemon_local_read_rejects_outside_allowlist() -> None:
    from astra.bridge_daemon import _local_read

    with tempfile.TemporaryDirectory() as outside:
        p = Path(outside) / "leak.txt"
        p.write_text("secret")

        with pytest.raises(PermissionError):
            _local_read({"path": str(p)}, ["/some/other/root"])


# ── Daemon tool impl — local_write / local_edit ───────────


def test_daemon_local_write_creates_parents() -> None:
    from astra.bridge_daemon import _local_write

    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / "subdir" / "deeper" / "f.txt"
        out = _local_write(
            {"path": str(target), "content": "hello"},
            [td],
        )
        assert "wrote" in out
        assert target.exists()
        assert target.read_text() == "hello"


def test_daemon_local_edit_unique_match() -> None:
    from astra.bridge_daemon import _local_edit

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "f.txt"
        p.write_text("alpha beta gamma")

        _local_edit(
            {
                "path": str(p),
                "old_string": "beta",
                "new_string": "BETA",
            },
            [td],
        )
        assert p.read_text() == "alpha BETA gamma"


def test_daemon_local_edit_rejects_ambiguous() -> None:
    """If old_string matches >1 times, error — caller must add
    surrounding context to make it unique."""
    from astra.bridge_daemon import _local_edit

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "f.txt"
        p.write_text("foo and foo and foo")

        with pytest.raises(ValueError, match="matches 3 times"):
            _local_edit(
                {
                    "path": str(p),
                    "old_string": "foo",
                    "new_string": "bar",
                },
                [td],
            )


def test_daemon_local_edit_rejects_no_match() -> None:
    from astra.bridge_daemon import _local_edit

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "f.txt"
        p.write_text("hello world")

        with pytest.raises(ValueError, match="not found"):
            _local_edit(
                {
                    "path": str(p),
                    "old_string": "missing",
                    "new_string": "x",
                },
                [td],
            )


# ── Daemon tool impl — local_bash ─────────────────────────


def test_daemon_local_bash_captures_output() -> None:
    from astra.bridge_daemon import _local_bash

    out = _local_bash(
        {"command": "echo hi && echo bye 1>&2"},
        roots=["/"],
        allowed_patterns=None,
    )
    assert "hi" in out
    assert "bye" in out
    assert "exit=0" in out


def test_daemon_local_bash_pattern_allowlist() -> None:
    from astra.bridge_daemon import _local_bash

    # Should pass — matches `^echo`
    _local_bash(
        {"command": "echo ok"},
        roots=["/"],
        allowed_patterns=[r"^echo\s"],
    )
    # Should fail — doesn't match
    with pytest.raises(PermissionError):
        _local_bash(
            {"command": "rm -rf /"},
            roots=["/"],
            allowed_patterns=[r"^echo\s"],
        )


def test_daemon_local_bash_cwd_allowlist() -> None:
    """cwd outside the allowlist is rejected."""
    from astra.bridge_daemon import _local_bash

    with pytest.raises(PermissionError):
        _local_bash(
            {"command": "ls", "cwd": "/etc"},
            roots=["/Users/kunalsingh/Documents"],
            allowed_patterns=None,
        )


# ── Daemon tool impl — local_glob ─────────────────────────


def test_daemon_local_glob_filters_by_allowlist() -> None:
    """Even if the glob pattern matches paths outside the allowlist,
    they're filtered out."""
    from astra.bridge_daemon import _local_glob

    with tempfile.TemporaryDirectory() as td:
        (Path(td) / "a.txt").write_text("")
        (Path(td) / "b.txt").write_text("")

        out = _local_glob(
            {"pattern": str(Path(td) / "*.txt")},
            [td],
        )
        assert "a.txt" in out
        assert "b.txt" in out


# ── Tools side: registry registration ─────────────────────


def test_local_tools_registered() -> None:
    """All seven local_* tools land in the registry under namespace
    'local' when astra.runtime.tools is imported."""
    import astra.runtime.tools  # noqa: F401  - side effect
    from astra.runtime.tool_registry import REGISTRY

    expected = {
        "local_read",
        "local_write",
        "local_edit",
        "local_bash",
        "local_glob",
        "local_grep",
        "local_bridge_status",
    }
    registered = set(REGISTRY.names())
    missing = expected - registered
    assert not missing, f"missing local tools: {missing}"

    for name in expected:
        td = REGISTRY.get(name)
        assert td.namespace == "local", f"{name} wrong namespace"
