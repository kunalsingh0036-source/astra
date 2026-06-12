"""
Railway ops — logs + restart across EVERY service, via the Railway API.

The "fix through Astra chat" control plane for deployment-level ops.
Railway is the actual control plane for all of Kunal's services —
both Tier-1 (Astra's own) and Tier-2 (federated agents in separate
projects) — so ONE Railway-API integration gives Astra logs +
restart over the whole fleet without editing five agent repos or
hacky self-restart endpoints.

Auth: a Railway ACCOUNT token (railway.com/account/tokens) in
RAILWAY_API_TOKEN — account-scoped so it spans every project. Absent
token → every tool degrades to one honest line, never a crash.

Tiers:
  agent_logs   — READ. Recent deployment logs for a service by name.
  restart_agent — DESTRUCTIVE. Redeploys a service. Routed through
                  the autonomy gate, so in always_ask/semi_auto it
                  asks Kunal first (the Phase C approval flow).

Service resolution is by NAME across all projects (case-insensitive),
so the agent says "restart apex" and this finds it wherever it lives.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from astra.runtime.sdk_compat import tool, create_sdk_mcp_server

logger = logging.getLogger(__name__)

_API = "https://backboard.railway.com/graphql/v2"
_TIMEOUT = 20.0


def _token() -> str:
    return os.environ.get("RAILWAY_API_TOKEN", "").strip()


async def _gql(query: str, variables: dict | None = None) -> dict | None:
    """Run a GraphQL op. Returns the `data` object, or None on any
    failure (logged). Never raises."""
    tok = _token()
    if not tok:
        return None
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.post(
                _API,
                headers={
                    "Authorization": f"Bearer {tok}",
                    "content-type": "application/json",
                },
                json={"query": query, "variables": variables or {}},
            )
        body = r.json()
        if body.get("errors"):
            logger.warning("[railway-ops] gql errors: %s", body["errors"][:1])
            return None
        return body.get("data")
    except Exception as e:
        logger.warning("[railway-ops] gql call failed: %s", e)
        return None


_RESOLVE_QUERY = """
query {
  projects {
    edges { node {
      name
      environments { edges { node { id name } } }
      services { edges { node { id name } } }
    } }
  }
}
"""


# Infra service names — never the thing a user means by "restart X".
# When a query matches a PROJECT, we pick the app service, not these.
_INFRA = {"postgres", "redis", "mysql", "mongo", "backup", "worker",
          "beat", "celery", "scheduler-worker"}


async def _resolve_service(name: str) -> dict | None:
    """Name → {service_id, environment_id, project, service} across
    all projects.

    Matches the way Kunal actually refers to agents — by SERVICE name
    OR PROJECT name ('linkedin' → project 'LinkedIn Agent' → its app
    service 'Backend'; 'apex' → 'apex-sales-team'). When the match is
    on the project (or ambiguous), picks the app service, never the
    Postgres/Redis/worker infra. Prefers a 'production' environment.
    Exact service-name match always wins.
    """
    data = await _gql(_RESOLVE_QUERY)
    if not data:
        return None
    want = name.strip().lower()
    candidates: list[dict] = []
    for pedge in data.get("projects", {}).get("edges", []):
        proj = pedge["node"]
        pname = proj["name"].lower()
        envs = [e["node"] for e in proj.get("environments", {}).get("edges", [])]
        env = next(
            (e for e in envs if e["name"].lower() == "production"),
            envs[0] if envs else None,
        )
        if not env:
            continue
        services = [se["node"] for se in proj.get("services", {}).get("edges", [])]
        app_services = [s for s in services if s["name"].lower() not in _INFRA]
        project_hit = (
            want in pname.replace(" ", "")
            or pname.replace(" ", "") in want
            or want in pname
        )
        for svc in services:
            sname = svc["name"].lower()
            svc_hit = want == sname or want in sname or sname in want
            # A project-name match resolves to the APP service only
            # (not infra); a direct service-name match always counts.
            if svc_hit or (
                project_hit and svc in app_services
                and (len(app_services) == 1 or sname not in _INFRA)
            ):
                candidates.append(
                    {
                        "service_id": svc["id"],
                        "service": svc["name"],
                        "environment_id": env["id"],
                        "project": proj["name"],
                        "exact": want == sname,
                        "is_app": svc in app_services,
                        # project-only matches rank below service matches
                        "svc_hit": svc_hit,
                    }
                )
    if not candidates:
        return None
    # Rank: exact service name > any service-name hit > app service > rest.
    candidates.sort(
        key=lambda c: (not c["exact"], not c["svc_hit"], not c["is_app"])
    )
    return candidates[0]


@tool(
    "agent_logs",
    "Recent deployment logs for any service/agent by name (across all "
    "Railway projects). Use to diagnose 'why is X down / erroring'. "
    "Read-only. Needs RAILWAY_API_TOKEN.",
    {"service": str, "lines": int},
)
async def agent_logs_tool(args: dict) -> dict:
    if not _token():
        return {
            "content": [
                {
                    "type": "text",
                    "text": "Railway ops not configured — set "
                    "RAILWAY_API_TOKEN (account token from "
                    "railway.com/account/tokens) on the stream service.",
                }
            ]
        }
    name = str(args.get("service", "")).strip()
    lines = int(args.get("lines", 50) or 50)
    target = await _resolve_service(name)
    if not target:
        return {
            "content": [{"type": "text", "text": f"No service matching {name!r}."}],
            "is_error": True,
        }
    # Latest deployment, then its logs.
    dep = await _gql(
        """
        query($sid: String!, $eid: String!) {
          deployments(first: 1, input: {serviceId: $sid, environmentId: $eid}) {
            edges { node { id status } }
          }
        }
        """,
        {"sid": target["service_id"], "eid": target["environment_id"]},
    )
    edges = (
        (dep or {}).get("deployments", {}).get("edges", []) if dep else []
    )
    if not edges:
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"{target['service']}: no deployments found.",
                }
            ]
        }
    deployment_id = edges[0]["node"]["id"]
    logs = await _gql(
        """
        query($id: String!, $limit: Int!) {
          deploymentLogs(deploymentId: $id, limit: $limit) { message }
        }
        """,
        {"id": deployment_id, "limit": min(max(lines, 1), 200)},
    )
    rows = (logs or {}).get("deploymentLogs", []) if logs else []
    if not rows:
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"{target['service']} ({target['project']}): "
                    "no recent log lines.",
                }
            ]
        }
    body = "\n".join(r.get("message", "") for r in rows)
    head = f"{target['service']} ({target['project']}) — last {len(rows)} log lines:\n"
    return {"content": [{"type": "text", "text": head + body[-4000:]}]}


@tool(
    "restart_agent",
    "Restart / redeploy a service or agent by name (across all Railway "
    "projects). DESTRUCTIVE — interrupts the running service. The "
    "autonomy gate will ask for approval first unless in full_auto. "
    "Needs RAILWAY_API_TOKEN.",
    {"service": str},
)
async def restart_agent_tool(args: dict) -> dict:
    if not _token():
        return {
            "content": [
                {
                    "type": "text",
                    "text": "Railway ops not configured — set "
                    "RAILWAY_API_TOKEN on the stream service.",
                }
            ]
        }
    name = str(args.get("service", "")).strip()
    target = await _resolve_service(name)
    if not target:
        return {
            "content": [{"type": "text", "text": f"No service matching {name!r}."}],
            "is_error": True,
        }
    data = await _gql(
        """
        mutation($sid: String!, $eid: String!) {
          serviceInstanceRedeploy(serviceId: $sid, environmentId: $eid)
        }
        """,
        {"sid": target["service_id"], "eid": target["environment_id"]},
    )
    if data is None:
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Restart of {target['service']} failed (API "
                    "error — see logs).",
                }
            ],
            "is_error": True,
        }
    return {
        "content": [
            {
                "type": "text",
                "text": f"Redeploy triggered for {target['service']} "
                f"({target['project']}). It'll be back in ~1-2 min; "
                "check fleet_status.",
            }
        ]
    }


def create_railway_ops_mcp_server():
    return create_sdk_mcp_server(
        name="astra-railway-ops",
        version="0.1.0",
        tools=[agent_logs_tool, restart_agent_tool],
    )
