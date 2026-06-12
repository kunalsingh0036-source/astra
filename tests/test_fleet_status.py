"""Unified fleet_status + liar-removal locks.

The liars (services namespace + agent_status/fleet_summary) probed a
decommissioned laptop topology / returned static 'active'. They must
stay gone, and fleet_status must be the single honest aggregator.
"""

from __future__ import annotations

import pytest


def test_liar_tools_unregistered():
    import astra.runtime.tools  # noqa: F401 — side-effect registration
    from astra.runtime.tool_registry import REGISTRY

    names = set(REGISTRY.names())
    liars = {
        "fleet_health", "fleet_status_legacy", "service_logs",
        "start_service", "stop_service", "start_fleet", "stop_fleet",
        "agent_status", "fleet_summary",
    }
    # The deleted ones (note: the NEW fleet_status is allowed)
    deleted = {
        "fleet_health", "service_logs", "start_service", "stop_service",
        "start_fleet", "stop_fleet", "agent_status", "fleet_summary",
    }
    present = deleted & names
    assert not present, f"liar tools still reachable by the agent: {present}"


def test_fleet_status_registered_in_business_namespace():
    import astra.runtime.tools  # noqa: F401
    from astra.runtime.tool_registry import REGISTRY

    ns = {t.name: t.namespace for t in REGISTRY.all()}
    assert ns.get("fleet_status") == "business"


def test_survivors_kept():
    """list_agents (catalogue) + recommend_agent (build planning) are
    static-but-honest and stay."""
    import astra.runtime.tools  # noqa: F401
    from astra.runtime.tool_registry import REGISTRY

    names = set(REGISTRY.names())
    assert "list_agents" in names
    assert "recommend_agent" in names


@pytest.mark.asyncio
async def test_fleet_status_degrades_to_honest_lines(monkeypatch):
    """Unreachable endpoints become honest status lines, never a crash
    and never a false 'healthy'."""
    monkeypatch.setenv("STREAM_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("EMAIL_AGENT_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("FINANCE_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("GATEWAY_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("A2A_BRIDGE_BASE", "http://127.0.0.1:1")
    monkeypatch.setenv("HELMTECH_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("APEX_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("APEX_EXPERIMENTAL_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("LINKEDIN_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("BOOKKEEPER_URL", "")

    from astra.tools.business_state_tools import fleet_status_tool

    out = await fleet_status_tool.handler({})
    text = out["content"][0]["text"]
    assert "FLEET STATUS" in text
    assert "Tier 1" in text and "Tier 2" in text
    assert "unreachable" in text  # dead endpoints reported honestly
    assert "not deployed" in text  # empty bookkeeper URL
    assert "healthy" not in text.split("Summary")[0].replace(
        "no HTTP health surface", ""
    )  # nothing falsely healthy
