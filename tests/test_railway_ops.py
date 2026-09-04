"""Railway-ops tool logic — service resolution, degradation, tiering.

The live GraphQL path can't run in CI (needs an account token), so we
mock _gql and test the resolution + formatting + safety logic, plus
lock the destructive tiering of restart_agent."""

from __future__ import annotations

import asyncio

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


# ── restart_agent target resolution (found 2026-09-04) ────
#
# `svc_hit = want == sname or want in sname or sname in want` and the
# matching project_hit made "" a substring of every name, so
# restart_agent({}) resolved to a real service and redeployed it — and
# `_INFRA` was only consulted on the PROJECT branch, so a direct hit on
# "postgres" went straight through to a production database. Across the
# 7 projects on this account that included other businesses'.

_FIXTURE = {"projects": {"edges": [
    {"node": {"name": "astra",
              "environments": {"edges": [{"node": {"id": "e1", "name": "production"}}]},
              "services": {"edges": [
                  {"node": {"id": "s1", "name": "stream"}},
                  {"node": {"id": "s2", "name": "agents"}},
                  {"node": {"id": "s3", "name": "Postgres"}},
                  {"node": {"id": "s4", "name": "Redis"}},
                  {"node": {"id": "s5", "name": "scheduler"}}]}}},
    {"node": {"name": "BAY",
              "environments": {"edges": [{"node": {"id": "e2", "name": "production"}}]},
              "services": {"edges": [
                  {"node": {"id": "b1", "name": "bay-nightly"}},
                  {"node": {"id": "b2", "name": "Postgres"}}]}}},
]}}


def _with_fixture(monkeypatch, redeploys=None):
    import astra.tools.railway_ops_tools as R

    async def fake_gql(q, v=None):
        if "serviceInstanceRedeploy" in q:
            if redeploys is not None:
                redeploys.append(v)
            return {"serviceInstanceRedeploy": True}
        return _FIXTURE

    monkeypatch.setattr(R, "_gql", fake_gql)
    monkeypatch.setenv("RAILWAY_API_TOKEN", "fake-for-test")
    return R


@pytest.mark.parametrize("probe", ["", " ", "a", "s", "pos", "ag"])
def test_resolver_refuses_names_too_short_to_be_specific(monkeypatch, probe):
    """The chokepoint both tools cross. An empty name matched EVERY
    service; 'a' and 's' each matched a real one."""
    R = _with_fixture(monkeypatch)
    assert asyncio.run(R._resolve_service(probe)) is None


@pytest.mark.parametrize("probe,expect", [
    ("stream", "stream"), ("scheduler", "scheduler"),
    ("bay-nightly", "bay-nightly"),
])
def test_resolver_still_finds_real_services(monkeypatch, probe, expect):
    R = _with_fixture(monkeypatch)
    t = asyncio.run(R._resolve_service(probe))
    assert t is not None and t["service"] == expect


def test_restart_refuses_short_and_empty(monkeypatch):
    redeploys = []
    R = _with_fixture(monkeypatch, redeploys)
    for probe in ["", "a", "pos"]:
        out = asyncio.run(R.restart_agent_tool.handler({"service": probe}))
        assert out.get("is_error") is True, f"{probe!r} was not refused"
        assert "too short or empty" in out["content"][0]["text"]
    assert redeploys == [], "a refused restart still fired a redeploy"


def test_restart_never_touches_a_database(monkeypatch):
    """_INFRA was only applied to project-name matches, so a direct hit
    on the service name went through and redeployed production
    Postgres — in whichever of the 7 projects matched first."""
    redeploys = []
    R = _with_fixture(monkeypatch, redeploys)
    for probe in ["postgres", "Postgres", "redis"]:
        out = asyncio.run(R.restart_agent_tool.handler({"service": probe}))
        assert out.get("is_error") is True, f"{probe!r} was not refused"
        assert "DATABASE" in out["content"][0]["text"]
    assert redeploys == [], "a database restart was actually issued"


def test_restart_still_works_for_a_legitimate_service(monkeypatch):
    """The guards must not break the tool — it is the only way Astra
    can recover a wedged service without Kunal at a terminal."""
    redeploys = []
    R = _with_fixture(monkeypatch, redeploys)
    out = asyncio.run(R.restart_agent_tool.handler({"service": "scheduler"}))
    assert out.get("is_error") is not True
    assert len(redeploys) == 1
