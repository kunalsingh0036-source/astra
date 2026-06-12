"""
Per-business operating pictures — honest v1.

One tool per business, each assembling what's REACHABLE today and
labelling what isn't. These are the substrate for the weekly
cross-business review ("where am I losing money or attention") and
the briefings' business lines. The rule inherited from the briefing
engine: a dead source is one honest clause, never a crash and never
fiction — this codebase's history of probing dead topologies and
reporting them as fleet health is exactly what these replace.

As agents grow real stats endpoints, each assembler picks them up
here — one place per business.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx

from astra.runtime.sdk_compat import tool, create_sdk_mcp_server

logger = logging.getLogger(__name__)

_TIMEOUT = 8.0


def _env(name: str, default: str) -> str:
    return os.environ.get(name, "").strip() or default


async def _get_json(url: str, headers: dict | None = None) -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(url, headers=headers or {})
            if r.status_code == 200:
                return r.json()
            return {"_status": r.status_code}
    except Exception:
        return None


def _mesh_headers() -> dict[str, str]:
    return {
        "x-astra-secret": os.environ.get("AGENT_SHARED_SECRET", "").strip()
    }


@tool(
    "helm_state",
    "HelmTech operating picture: outreach-agent health + WhatsApp "
    "gateway send stats. Use for 'how is HelmTech doing', briefings, "
    "and the weekly review. Compass priority #1.",
    {},
)
async def helm_state_tool(args: dict) -> dict:
    lines = ["HelmTech state:"]
    h = await _get_json(
        _env("HELMTECH_URL", "https://helm-sales-production.up.railway.app")
        + "/health"
    )
    lines.append(
        f"- outreach agent: {h.get('status', 'unreachable') if h else 'unreachable'}"
    )
    # WhatsApp gateway is HelmTech's primary outbound channel today
    # (same Meta app/number).
    wa = await _get_json(
        _env("GATEWAY_URL", "http://whatsapp.railway.internal:8080")
        + "/api/v1/conversations/stats",
        headers=_mesh_headers(),
    )
    if wa and "_status" not in wa:
        lines.append(f"- whatsapp gateway: {wa}")
    else:
        wa_h = await _get_json(
            _env("GATEWAY_URL", "http://whatsapp.railway.internal:8080")
            + "/health"
        )
        lines.append(
            "- whatsapp gateway: "
            + (wa_h.get("status", "unreachable") if wa_h else "unreachable")
            + " (no stats endpoint yet)"
        )
    lines.append(
        "- revenue data: not wired (no stats API on the agent yet — "
        "this line gets real when HelmTech exposes one)"
    )
    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


@tool(
    "apex_state",
    "Apex operating picture: B2B sales agent + Apex Experimental D2C "
    "backend health. Compass priority #2.",
    {},
)
async def apex_state_tool(args: dict) -> dict:
    lines = ["Apex state:"]
    sales = await _get_json(
        _env(
            "APEX_URL",
            "https://apex-sales-team-production-2c45.up.railway.app",
        )
        + "/health"
    )
    if sales and "_status" not in sales:
        lines.append(
            f"- B2B sales agent: {sales.get('status')} "
            f"(db={sales.get('database')}, redis={sales.get('redis')})"
        )
    else:
        lines.append("- B2B sales agent: unreachable")
    exp = await _get_json(
        _env(
            "APEX_EXPERIMENTAL_URL",
            "https://apex-experimental-production.up.railway.app",
        )
        + "/health"
    )
    if exp and "_status" not in exp:
        storage = (exp.get("storage") or {}).get("ok")
        lines.append(
            f"- Experimental D2C backend: {exp.get('status')} "
            f"(storage={'ok' if storage else 'DOWN'})"
        )
    else:
        lines.append("- Experimental D2C backend: unreachable")
    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


@tool(
    "bay_state",
    "BAY / squash operating picture: training debt, pending catch-up "
    "approvals, countdown to National Championships (Nov 16-22 2026). "
    "Compass priority #3 + the Olympic-gold ambition's proof point.",
    {},
)
async def bay_state_tool(args: dict) -> dict:
    from sqlalchemy import text as _sql

    from astra.db.engine import async_session

    lines = ["BAY state:"]
    nationals = date(2026, 11, 16)
    days = (nationals - datetime.now(timezone.utc).date()).days
    lines.append(f"- National Championships: {days} days out (Nov 16-22)")
    try:
        async with async_session() as s:
            r = await s.execute(
                _sql(
                    """
                    SELECT snapshot_date, stretch, meditate, breathe,
                           movement, skill, workout
                    FROM missed_session_snapshots
                    ORDER BY snapshot_date DESC LIMIT 1
                    """
                )
            )
            row = r.first()
            pend = (
                await s.execute(
                    _sql(
                        "SELECT count(*) FROM catchup_approvals "
                        "WHERE status = 'pending'"
                    )
                )
            ).scalar() or 0
        if row:
            debts = dict(
                zip(
                    ("stretch", "meditate", "breathe", "movement", "skill", "workout"),
                    row[1:],
                )
            )
            owed = {k: v for k, v in debts.items() if (v or 0) > 0}
            age = (datetime.now(timezone.utc).date() - row[0]).days
            lines.append(
                f"- training debt ({age}d-old snapshot): "
                + (", ".join(f"{k}×{v}" for k, v in owed.items()) or "clear")
            )
        else:
            lines.append("- training debt: no snapshot yet")
        if pend:
            lines.append(f"- {pend} catch-up submission(s) pending approval on /tonight")
    except Exception as e:
        logger.warning("[bay_state] db read failed: %s", e)
        lines.append("- training data unavailable")
    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


@tool(
    "topstudios_state",
    "Top Studios operating picture: recent creative output from the "
    "artifact store + kit status. Compass priority #4.",
    {},
)
async def topstudios_state_tool(args: dict) -> dict:
    from sqlalchemy import text as _sql

    from astra.db.engine import async_session

    lines = ["Top Studios state:"]
    try:
        since = datetime.now(timezone.utc) - timedelta(days=7)
        async with async_session() as s:
            r = await s.execute(
                _sql(
                    """
                    SELECT kind, count(*) FROM creator_artifacts
                    WHERE business_slug = 'top-studios' AND created_at >= :since
                    GROUP BY kind
                    """
                ),
                {"since": since},
            )
            rows = r.fetchall()
        if rows:
            lines.append(
                "- last 7d output: "
                + ", ".join(f"{k}×{n}" for k, n in rows)
            )
        else:
            lines.append("- last 7d output: none")
    except Exception:
        lines.append("- artifact store unavailable")
    lines.append(
        "- voice/kit: locked (intense·immersive·imaginative, 2026-05-19)"
    )
    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


# ── Unified fleet status ───────────────────────────────────
#
# THE single honest "how is everything" answer. Replaces the
# laptop-era liars: the `services` namespace (service_manager probing
# dead localhost:80xx — source of the "down/working-directory-missing"
# fiction) and the fleet-registry tools that returned a STATIC "active"
# from each agent card regardless of real health.
#
# Two tiers, matching the actual architecture:
#   Tier 1 — direct children: services deployed FROM the astra repo,
#            inside the astra Railway project. Probed over Railway's
#            private network (railway.internal).
#   Tier 2 — federated appendages: separate repos + separate Railway
#            projects, bridged via A2A. Probed over their public URLs.
# A dead source becomes one honest line, never fiction.

_TIER1 = {
    "stream": "STREAM_URL|http://stream.railway.internal:8080",
    "scheduler": None,  # no HTTP surface; reported via jobstore elsewhere
    "email": "EMAIL_AGENT_URL|http://email.railway.internal:8080",
    "finance": "FINANCE_URL|http://finance.railway.internal:8080",
    "whatsapp": "GATEWAY_URL|http://whatsapp.railway.internal:8080",
    "bridge": "A2A_BRIDGE_BASE|http://bridge.railway.internal:8500",
}
_TIER2 = {
    "helmtech": "HELMTECH_URL|https://helm-sales-production.up.railway.app",
    "apex-sales": "APEX_URL|https://apex-sales-team-production-2c45.up.railway.app",
    "apex-experimental": "APEX_EXPERIMENTAL_URL|https://apex-experimental-production.up.railway.app",
    "linkedin": "LINKEDIN_URL|https://backend-production-a2994.up.railway.app",
    "bookkeeper": "BOOKKEEPER_URL|",  # not deployed; honest 'not deployed'
}


def _resolve(spec: str | None) -> str:
    if not spec:
        return ""
    env_name, _, default = spec.partition("|")
    return os.environ.get(env_name, "").strip() or default


async def _probe(name: str, spec: str | None) -> tuple[str, str]:
    url = _resolve(spec)
    if spec is None:
        return name, "no HTTP health surface"
    if not url:
        return name, "not deployed"
    data = await _get_json(url.rstrip("/") + "/health")
    if data is None:
        return name, "unreachable"
    if "_status" in data:
        return name, f"HTTP {data['_status']}"
    return name, str(data.get("status", "ok"))


@tool(
    "fleet_status",
    "THE single honest status of every service + agent connected to "
    "Astra. Use for 'how is everything', 'is anything down', 'fleet "
    "status', or any whole-system health question. Covers Tier-1 "
    "direct children (stream/scheduler/email/finance/whatsapp/bridge) "
    "and Tier-2 federated agents (helmtech/apex/linkedin/bookkeeper). "
    "Prefer this over any service_* / agent_status / fleet_summary "
    "tool — those probe a decommissioned laptop topology and lie.",
    {},
)
async def fleet_status_tool(args: dict) -> dict:
    import asyncio

    t1 = await asyncio.gather(*[_probe(n, s) for n, s in _TIER1.items()])
    t2 = await asyncio.gather(*[_probe(n, s) for n, s in _TIER2.items()])

    def _healthy(v: str) -> bool:
        return v in ("ok", "healthy")

    lines = ["FLEET STATUS\n", "Tier 1 — Astra's own services:"]
    for n, v in t1:
        mark = "✓" if _healthy(v) else ("·" if "no HTTP" in v else "✗")
        lines.append(f"  {mark} {n}: {v}")
    lines.append("\nTier 2 — federated agents (separate projects, via A2A):")
    for n, v in t2:
        mark = "✓" if _healthy(v) else ("·" if v == "not deployed" else "✗")
        lines.append(f"  {mark} {n}: {v}")

    down = [n for n, v in (*t1, *t2) if not _healthy(v) and v not in ("not deployed", "no HTTP health surface")]
    healthy_ct = sum(1 for _, v in (*t1, *t2) if _healthy(v))
    total = len(t1) + len(t2)
    lines.append(
        f"\nSummary: {healthy_ct}/{total} healthy"
        + (f" · DOWN: {', '.join(down)}" if down else " · all green")
    )
    # Local bridge daemon (the Mac connection) — distinct from the
    # cloud `bridge` A2A service above. "Online" = a bridge token was
    # seen polling within the last 2 minutes (it long-polls ~every
    # few seconds when the Mac is awake).
    try:
        from sqlalchemy import text as _sql2

        from astra.db.engine import async_session

        async with async_session() as s:
            recent = (
                await s.execute(
                    _sql2(
                        "SELECT count(*) FROM bridge_tokens "
                        "WHERE last_seen_at > now() - interval '2 minutes'"
                    )
                )
            ).scalar() or 0
        lines.append(
            "Local bridge daemon: "
            + ("online" if recent else "offline (Mac asleep / not running)")
        )
    except Exception:
        pass
    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


def create_business_state_mcp_server():
    return create_sdk_mcp_server(
        name="astra-business-state",
        version="0.1.0",
        tools=[
            helm_state_tool,
            apex_state_tool,
            bay_state_tool,
            topstudios_state_tool,
            fleet_status_tool,
        ],
    )
