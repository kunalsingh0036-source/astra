"""Unified 'agents' service — the appendages under one process.

R3 (backend consolidation): instead of four always-on services (email,
finance, whatsapp-gateway, a2a-bridge) — each its own deploy, build, and
independent failure point (one bad transitive dep = four fleet bombs) —
mount them as sub-apps on ONE FastAPI process. Each sub-app keeps its own
routes, middleware/auth, and DB engine; mounting changes the deploy
topology, not a line of their behaviour.

The two CORES stay separate on purpose: `stream` (the chat/agent runtime —
never couple it to appendage imports) and `scheduler` (an APScheduler loop,
a different process model). `web` (Node, separate repo), Postgres, Redis,
and the `backup` cron also stay as-is. Net: 7 backend services → 3
(stream + scheduler + agents).

CUTOVER IS PARALLEL, NOT BIG-BANG: deploy this beside the existing four,
repoint each consumer URL (EMAIL_AGENT_URL → …/email, FINANCE_URL →
…/finance, GATEWAY_URL → …/whatsapp, A2A_BRIDGE_BASE → …/a2a) one at a
time with verification, then decommission the old four. Rollback at any
step = point the env var back; the old service stays up until the end.

Start command must replicate the email service's credential prep:
  sh -c 'echo "$GMAIL_CREDENTIALS_JSON" > /tmp/gmail-creds.json && \
         echo "$GMAIL_TOKEN_JSON" > /tmp/gmail-token.json && cd /app && \
         exec uvicorn services.agents.main:app --host 0.0.0.0 --port ${PORT:-8080}'
"""

from __future__ import annotations

from fastapi import FastAPI

app = FastAPI(
    title="Astra agents (unified appendages)",
    description="email + finance + whatsapp + a2a-bridge on one process",
    version="0.1.0",
)


@app.get("/health")
async def health() -> dict:
    return {
        "status": "healthy",
        "service": "agents",
        "mounts": ["/finance", "/a2a"],
    }


# ── Sub-apps. Each keeps its own routes + middleware/auth + DB engine. ──
# Mounted under a path prefix; mounted sub-apps run their own lifespan and
# middleware for requests under their mount, so behaviour is unchanged —
# only the base URL moves (host:port → host:port/<prefix>).
from services.finance.main import app as _finance_app  # noqa: E402

from astra.agents.external.bridge_server import app as _bridge_app  # noqa: E402

app.mount("/finance", _finance_app)
app.mount("/a2a", _bridge_app)

# email + whatsapp are NOT folded here yet. Each has an EXTERNAL inbound
# webhook bound to its public domain (Gmail Pub/Sub → email.thearrogant
# club.com/api/v1/webhook/gmail; Meta → whatsapp.thearrogantclub.com).
# A prefixed sub-app (/email, /whatsapp) moves those paths, breaking
# inbound mail + WhatsApp until the Pub/Sub push URL and the Meta webhook
# are reconfigured (Kunal's console actions). Fold them in a follow-up:
#   from services.email_agent.main import app as _email_app
#   from services.gateway.main import app as _gateway_app
#   app.mount("/email", _email_app); app.mount("/whatsapp", _gateway_app)
# then repoint EMAIL_AGENT_URL/EMAIL_URL/GATEWAY_URL + the external webhooks.
