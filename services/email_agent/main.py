"""Email Agent — FastAPI application."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from email_agent.config import settings
from email_agent.api.routes import (
    accounts,
    ai,
    contacts,
    drafts,
    messages,
    scheduled,
    templates,
    threads,
    webhook,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events."""
    import email_agent.models  # noqa: F401
    # Detect ngrok URL on startup
    await _detect_ngrok_url()
    yield


async def _detect_ngrok_url():
    """Check if ngrok is running and store the public URL."""
    import httpx
    import logging
    logger = logging.getLogger(__name__)
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get("http://localhost:4040/api/tunnels", timeout=3)
            tunnels = resp.json().get("tunnels", [])
        for t in tunnels:
            if "https" in t.get("public_url", ""):
                settings.webhook_base_url = t["public_url"]
                logger.info("ngrok detected: %s", t["public_url"])
                return
    except Exception:
        pass  # ngrok not running — that's fine


app = FastAPI(
    title="Email Agent",
    description="AI-powered email management for Astra fleet",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Mesh auth ───────────────────────────────────────────────
#
# This service fronts Kunal's real mailbox: read archive, send-as.
# Until 2026-06-11 it sat on the public internet with NO auth — any
# caller could list messages or send email as Kunal. Every /api/v1/*
# request now requires `x-astra-secret` matching AGENT_SHARED_SECRET.
#
# Public (no secret):
#   - /health, /            — fleet probes + Railway healthcheck
#   - POST /api/v1/webhook/gmail — Google Pub/Sub push. Google can't
#     send our header; the handler only triggers a refetch from Gmail
#     (no attacker-controlled data is stored verbatim), so a forged
#     push costs us an API call, not integrity.
#
# FAIL CLOSED: if AGENT_SHARED_SECRET is unset, protected routes
# return 503 rather than going open — the empty-env-save failure
# class is documented in learnings_railway_migration.md.

# Exact paths only — /api/v1/webhook/gmail/diag and /gmail/watch are
# operator endpoints and stay protected; only Google's push target is
# open.
_PUBLIC_EXACT = {"/", "/health", "/api/v1/webhook/gmail"}


@app.middleware("http")
async def require_mesh_secret(request, call_next):
    import hmac as _hmac

    from fastapi.responses import JSONResponse

    path = request.url.path.rstrip("/") or "/"
    if request.method == "OPTIONS" or path in _PUBLIC_EXACT:
        return await call_next(request)
    secret = settings.agent_shared_secret.strip()
    if not secret:
        return JSONResponse(
            {"detail": "auth not configured: AGENT_SHARED_SECRET is unset"},
            status_code=503,
        )
    provided = request.headers.get("x-astra-secret", "").strip()
    if not _hmac.compare_digest(provided, secret):
        return JSONResponse({"detail": "unauthorized"}, status_code=401)
    return await call_next(request)

# Mount all API routes under /api/v1
for route_module in [
    accounts, messages, threads, contacts, templates, drafts, scheduled, ai, webhook,
]:
    app.include_router(route_module.router, prefix="/api/v1")


@app.get("/health")
async def health():
    return {"status": "healthy", "service": "email-agent", "port": settings.port}


@app.get("/")
async def root():
    return {
        "service": "email-agent",
        "version": "0.1.0",
        "endpoints": {
            "health": "/health",
            "docs": "/docs",
            "accounts": "/api/v1/accounts",
            "messages": "/api/v1/messages",
            "threads": "/api/v1/threads",
            "contacts": "/api/v1/contacts",
            "templates": "/api/v1/templates",
            "drafts": "/api/v1/drafts",
            "scheduled": "/api/v1/scheduled",
            "ai": "/api/v1/ai",
        },
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "email_agent.main:app",
        host=settings.host,
        port=settings.port,
        reload=True,
    )
