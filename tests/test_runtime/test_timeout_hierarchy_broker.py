"""Timeout hierarchy for the two broker-backed tools.

Outer >= inner + margin (docs/timeout_hierarchy.md). Both numbers in
astra/runtime/sdk_adapter.py SLOW_EXACT are DERIVED from constants the
tools own, and this test pins the derivation so a change to either
side without the other goes red:

  ingest_voice_export: registry >= reply_tools._INGEST_TOOL_SEC
      = pages x (per-page wait + overhead) + corpus POST + 20 s;
      the paged reader's page count stays under the broker's auto
      ceiling per hour and its page size IS the executor's ceiling.
  notes_sync: registry >= notes_tools._CHAT_WAIT_SEC + 20 s. The
      first version had no entry, so the 15 s default cancelled a
      90 s wait (invisible only while notes.sync was refused before
      filing).
  both: the 240 s turn cap >= registry + 60 s.

tests/test_runtime/test_timeout_hierarchy.py owns the rest of the
hierarchy; this file adds the broker rows without editing it.
"""

from __future__ import annotations

from astra.broker import client
from astra.runtime import sdk_adapter
from astra.runtime.agent_loop import _TURN_HARD_TIMEOUT_SEC
from astra.tools import notes_tools, reply_tools

_TURN_MARGIN = 60
_TOOL_MARGIN = 20


def _registry(name: str) -> int:
    return sdk_adapter._guess_timeout(name, "any")


def test_ingest_registry_budget_covers_the_derived_inner_budget():
    inner = reply_tools._INGEST_TOOL_SEC
    assert inner == (
        reply_tools._READ_MAX_PAGES
        * (reply_tools._READ_WAIT_SEC + reply_tools._READ_PAGE_OVERHEAD_SEC)
        + reply_tools._INGEST_POST_SEC + _TOOL_MARGIN
    )
    assert reply_tools._READ_PHASE_SEC == reply_tools._READ_MAX_PAGES * (
        reply_tools._READ_WAIT_SEC + reply_tools._READ_PAGE_OVERHEAD_SEC
    )
    assert _registry("ingest_voice_export") >= inner, (
        f"registry {_registry('ingest_voice_export')} < derived inner {inner}: "
        "the registry cancels the reader mid-loop and every page is wasted"
    )


def test_ingest_registry_budget_sits_under_the_turn_cap_with_margin():
    assert _TURN_HARD_TIMEOUT_SEC >= _registry("ingest_voice_export") + _TURN_MARGIN


def test_ingest_paging_respects_the_executor_and_broker_ceilings():
    assert reply_tools._READ_PAGE_BYTES == client.FS_READ_MAX_LIMIT
    assert reply_tools._READ_MAX_PAGES < client.AUTO_INTENTS_PER_HOUR
    # Per-page wait is derived from the measured 6-12 s loop latency:
    # at least the measured worst, at most a few multiples of it.
    assert 12 <= reply_tools._READ_WAIT_SEC <= 48


def test_notes_sync_registry_budget_covers_the_chat_wait():
    assert _registry("notes_sync") >= notes_tools._CHAT_WAIT_SEC + _TOOL_MARGIN
    assert _TURN_HARD_TIMEOUT_SEC >= _registry("notes_sync") + _TURN_MARGIN


def test_the_registered_tools_carry_these_budgets():
    import astra.runtime.tools  # noqa: F401
    from astra.runtime.tool_registry import REGISTRY

    assert REGISTRY.get("ingest_voice_export").timeout_sec == _registry("ingest_voice_export")
    assert REGISTRY.get("notes_sync").timeout_sec == _registry("notes_sync")
