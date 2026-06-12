"""agent_repos map — the anti-path-guessing substrate for code fixes."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_repo_map_covers_every_federated_agent():
    from astra.tools.agent_repos_tools import AGENT_REPOS, agent_repos_tool

    for name in ("helmtech", "apex-sales", "apex-experimental", "linkedin", "bookkeeper"):
        assert name in AGENT_REPOS, f"{name} missing from repo map"
        assert AGENT_REPOS[name]["path"].endswith(
            ("agent", "team", "experimental")
        )
    out = await agent_repos_tool.handler({})
    text = out["content"][0]["text"]
    assert "Fix flow:" in text  # the playbook is surfaced
    assert "gated" in text


def test_repo_root_is_env_overridable(monkeypatch):
    """ASTRA_CODE_ROOT lets the map work on any checkout location —
    no hardcoded laptop path baked into behaviour (the class of bug
    that broke calendar + timeout tests)."""
    monkeypatch.setenv("ASTRA_CODE_ROOT", "/tmp/code")
    import importlib

    import astra.tools.agent_repos_tools as m
    importlib.reload(m)
    assert m.AGENT_REPOS["helmtech"]["path"] == "/tmp/code/helmtech-outreach-agent"
    # restore default for other tests
    monkeypatch.delenv("ASTRA_CODE_ROOT", raising=False)
    importlib.reload(m)


def test_registered():
    import astra.runtime.tools  # noqa: F401
    from astra.runtime.tool_registry import REGISTRY

    assert "agent_repos" in REGISTRY.names()
