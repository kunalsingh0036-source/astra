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
