"""Railway-ops tool logic — service resolution, degradation, tiering.

The live GraphQL path can't run in CI (needs an account token), so we
mock _gql and test the resolution + formatting + safety logic, plus
lock the destructive tiering of restart_agent."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_tools_degrade_without_token(monkeypatch):
    monkeypatch.delenv("RAILWAY_API_TOKEN", raising=False)
    from astra.tools import railway_ops_tools as r

    logs = await r.agent_logs_tool.handler({"service": "apex"})
    assert "not configured" in logs["content"][0]["text"].lower()
    rs = await r.restart_agent_tool.handler({"service": "apex"})
    assert "not configured" in rs["content"][0]["text"].lower()


@pytest.mark.asyncio
async def test_resolve_matches_substring_and_prefers_exact(monkeypatch):
    monkeypatch.setenv("RAILWAY_API_TOKEN", "tok")
    from astra.tools import railway_ops_tools as r

    fake = {
        "projects": {"edges": [
            {"node": {
                "name": "Apex Sales",
                "environments": {"edges": [
                    {"node": {"id": "env-prod", "name": "production"}},
                    {"node": {"id": "env-dev", "name": "dev"}},
                ]},
                "services": {"edges": [
                    {"node": {"id": "svc-apex", "name": "apex-sales-team"}},
                ]},
            }},
            {"node": {
                "name": "astra",
                "environments": {"edges": [
                    {"node": {"id": "env-a", "name": "production"}},
                ]},
                "services": {"edges": [
                    {"node": {"id": "svc-stream", "name": "stream"}},
                    {"node": {"id": "svc-apex2", "name": "apex"}},
                ]},
            }},
        ]}
    }

    async def _fake_gql(q, v=None):
        return fake

    monkeypatch.setattr(r, "_gql", _fake_gql)

    # 'apex' exact-matches the 'apex' service over the substring
    # 'apex-sales-team'
    t = await r._resolve_service("apex")
    assert t["service"] == "apex"
    assert t["environment_id"] == "env-a"

    # substring still resolves when no exact
    t2 = await r._resolve_service("sales-team")
    assert t2["service"] == "apex-sales-team"
    assert t2["environment_id"] == "env-prod"  # prefers production


@pytest.mark.asyncio
async def test_resolve_prefers_production_env(monkeypatch):
    monkeypatch.setenv("RAILWAY_API_TOKEN", "tok")
    from astra.tools import railway_ops_tools as r

    fake = {"projects": {"edges": [{"node": {
        "name": "P",
        "environments": {"edges": [
            {"node": {"id": "dev", "name": "dev"}},
            {"node": {"id": "prod", "name": "production"}},
        ]},
        "services": {"edges": [{"node": {"id": "s", "name": "thing"}}]},
    }}]}}

    async def _fake_gql(q, v=None):
        return fake
    monkeypatch.setattr(r, "_gql", _fake_gql)
    t = await r._resolve_service("thing")
    assert t["environment_id"] == "prod"


def test_restart_is_destructive_tier():
    """The gate must ASK before a restart — locked in modes.TOOL_TIERS."""
    from astra.autonomy.modes import TOOL_TIERS, ActionTier

    assert TOOL_TIERS["restart_agent"] == ActionTier.DESTRUCTIVE
    assert TOOL_TIERS["agent_logs"] == ActionTier.READ


@pytest.mark.asyncio
async def test_resolve_matches_project_name_to_app_service(monkeypatch):
    """'linkedin' must resolve to the LinkedIn project's APP service
    ('Backend'), not fail and not pick Postgres/Redis. This was a real
    miss: the resolver only matched service names, but Kunal refers to
    agents by project/agent name."""
    monkeypatch.setenv("RAILWAY_API_TOKEN", "tok")
    from astra.tools import railway_ops_tools as r

    fake = {"projects": {"edges": [{"node": {
        "name": "LinkedIn Agent",
        "environments": {"edges": [{"node": {"id": "e", "name": "production"}}]},
        "services": {"edges": [
            {"node": {"id": "pg", "name": "Postgres"}},
            {"node": {"id": "rd", "name": "Redis"}},
            {"node": {"id": "be", "name": "Backend"}},
        ]},
    }}]}}

    async def _fake_gql(q, v=None):
        return fake
    monkeypatch.setattr(r, "_gql", _fake_gql)

    t = await r._resolve_service("linkedin")
    assert t is not None, "linkedin should resolve via project name"
    assert t["service"] == "Backend", f"picked infra, not app: {t['service']}"


@pytest.mark.asyncio
async def test_resolve_never_picks_infra_for_project_match(monkeypatch):
    monkeypatch.setenv("RAILWAY_API_TOKEN", "tok")
    from astra.tools import railway_ops_tools as r

    fake = {"projects": {"edges": [{"node": {
        "name": "HelmTech Sales",
        "environments": {"edges": [{"node": {"id": "e", "name": "production"}}]},
        "services": {"edges": [
            {"node": {"id": "pg", "name": "Postgres"}},
            {"node": {"id": "h", "name": "Helm-Sales"}},
        ]},
    }}]}}

    async def _fake_gql(q, v=None):
        return fake
    monkeypatch.setattr(r, "_gql", _fake_gql)

    t = await r._resolve_service("helmtech")
    assert t["service"] == "Helm-Sales"
