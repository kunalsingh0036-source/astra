"""The self-improve scan must keep excluding the e2e harness's sessions.

scripts/e2e_smoke.py tags every turn it starts with
HARNESS_SESSION_PREFIX, and jobs.py::self_improve_scan excludes those
sessions when it counts expired approvals (test 16 leaves one pending
on purpose). The two literals live in different files, and if either
side drifts the harness quietly starts filing "over-asking"
observations again. This pins them together.
"""

from __future__ import annotations

import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[1]


def _harness_prefix() -> str:
    src = (REPO / "scripts/e2e_smoke.py").read_text(encoding="utf-8")
    m = re.search(r'^HARNESS_SESSION_PREFIX = "([^"]+)"$', src, re.M)
    assert m, "scripts/e2e_smoke.py no longer defines HARNESS_SESSION_PREFIX"
    return m.group(1)


def test_every_harness_turn_carries_the_prefix():
    src = (REPO / "scripts/e2e_smoke.py").read_text(encoding="utf-8")
    body = src[src.index("async def _post_chat_stream"):]
    body = body[: body.index("\nasync def ", 1)]
    assert "HARNESS_SESSION_PREFIX" in body, (
        "_post_chat_stream must default the session id to the prefix"
    )


def test_self_improve_scan_excludes_the_harness_prefix():
    prefix = _harness_prefix()
    src = (REPO / "astra/scheduler/jobs.py").read_text(encoding="utf-8")
    body = src[src.index("async def self_improve_scan"):]
    assert f"NOT LIKE '{prefix}%'" in body, (
        "self_improve_scan's expired-approvals count must exclude "
        f"sessions starting with {prefix!r}"
    )
