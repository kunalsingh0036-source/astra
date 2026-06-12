"""
WhatsApp Gateway — FastAPI application entry point.

Mounts all routers:
- /api/v1/send — Outbound message endpoint
- /api/v1/webhook — Meta webhook receiver
- /api/v1/conversations — Conversation queries
- /api/v1/templates — Template management
- /api/v1/stats — Gateway statistics
- /a2a/* — A2A protocol endpoints
- /.well-known/agent.json — A2A discovery
"""

import logging
from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import FastAPI

from gateway.config import settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan — setup and teardown."""
    # Startup: create shared HTTP client
    app.state.http_client = httpx.AsyncClient(timeout=30.0)
    logger.info(
        f"WhatsApp Gateway starting on {settings.host}:{settings.port}"
    )
    yield
    # Shutdown: close HTTP client
    await app.state.http_client.aclose()
    logger.info("WhatsApp Gateway shut down")


app = FastAPI(
    title="WhatsApp Gateway",
    description="Unified WhatsApp messaging for the Astra agent fleet",
    version="0.1.0",
    lifespan=lifespan,
)

# ── Mesh auth ───────────────────────────────────────────────
#
# This service sends WhatsApp as Kunal's businesses. Until 2026-06-11
# it sat on the public internet with NO auth — anyone could POST
# /api/v1/send. Every protected request now requires `x-astra-secret`
# matching AGENT_SHARED_SECRET.
#
# Public (no secret):
#   - /health                 — fleet probes + Railway healthcheck
#   - /api/v1/webhook         — Meta's inbound traffic. GET is the
#     hub.verify_token challenge; POST is HMAC-signature-verified in
#     the handler (X-Hub-Signature-256). Meta can't send our header.
#   - /.well-known/agent.json — A2A discovery card, public metadata.
#
# FAIL CLOSED: if AGENT_SHARED_SECRET is unset, protected routes
# return 503 rather than going open.

_PUBLIC_EXACT = {"/health", "/api/v1/webhook", "/.well-known/agent.json"}


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


# Mount API routers
from gateway.api.send import router as send_router
from gateway.api.webhook import router as webhook_router
from gateway.api.conversations import router as conversations_router
from gateway.api.templates import router as templates_router
from gateway.api.queue import router as queue_router
from gateway.api.notify import router as notify_router

app.include_router(send_router)
app.include_router(webhook_router)
app.include_router(conversations_router)
app.include_router(templates_router)
app.include_router(queue_router)
app.include_router(notify_router)

# Mount A2A protocol routers
try:
    from gateway.a2a.agent import whatsapp_agent
    app.include_router(whatsapp_agent.router)
    app.include_router(whatsapp_agent.well_known_router)
    logger.info("A2A agent routes mounted")
except ImportError:
    logger.warning("A2A module not available — running without Astra integration")


@app.get("/health")
async def health():
    """Basic health check."""
    return {
        "status": "healthy",
        "service": "whatsapp-gateway",
        "version": "0.1.0",
        "meta_configured": bool(
            settings.whatsapp_access_token and settings.whatsapp_phone_number_id
        ),
    }


def start():
    """Entry point for running the server."""
    uvicorn.run(
        "gateway.main:app",
        host=settings.host,
        port=settings.port,
        reload=True,
    )


if __name__ == "__main__":
    start()
