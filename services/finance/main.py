"""Finance Agent — FastAPI application."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from finance.config import settings
from finance.api.routes import (
    ai,
    alerts,
    bank_accounts,
    businesses,
    cash_flow,
    dashboard,
    expenses,
    invoices,
    payments,
    reconciliation,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events."""
    # Import models so they register with SQLAlchemy metadata
    import finance.models  # noqa: F401

    yield


app = FastAPI(
    title="Finance Agent",
    description="AI-powered financial management for Kunal's businesses",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    # NOT "*": this API serves five companies' financial records. Only the
    # astra-web origin (and localhost for dev) may talk to it from a browser.
    allow_origins=[settings.web_origin, "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Mesh auth ──────────────────────────────────────────────────────
#
# Until 2026-08-07 this service had NO authentication: unauthenticated
# GETs returned data and unauthenticated POSTs reached the handlers.
# It was harmless only because every table was empty; seeding the entity
# master (GSTIN/PAN/CIN) would have published it. Every sibling service
# (email_agent, gateway, stream) already had this. Pattern copied from
# services/email_agent/main.py.
#
# FAIL CLOSED: unset secret → 503 on protected routes, never open.
_PUBLIC_EXACT = {"/", "/health"}


@app.middleware("http")
async def require_mesh_secret(request, call_next):
    import hmac as _hmac

    from fastapi.responses import JSONResponse

    path = request.url.path.rstrip("/") or "/"
    # This app is mounted at /finance inside the `agents` service. Depending
    # on Starlette's root_path handling the prefix may or may not appear in
    # request.url.path, so normalise both shapes — otherwise /health is
    # protected in one deployment shape and everything is public in the other.
    if path.startswith("/finance"):
        path = path[len("/finance"):].rstrip("/") or "/"

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
    businesses, invoices, payments, expenses, bank_accounts,
    alerts, reconciliation, cash_flow, dashboard, ai,
]:
    app.include_router(route_module.router, prefix="/api/v1")


@app.get("/health")
async def health():
    return {"status": "healthy", "service": "finance-agent", "port": settings.port}


@app.get("/")
async def root():
    return {
        "service": "finance-agent",
        "version": "0.1.0",
        "endpoints": {
            "health": "/health",
            "docs": "/docs",
            "businesses": "/api/v1/businesses",
            "invoices": "/api/v1/invoices",
            "payments": "/api/v1/payments",
            "expenses": "/api/v1/expenses",
            "bank_accounts": "/api/v1/bank-accounts",
            "reconciliation": "/api/v1/reconciliation",
            "cash_flow": "/api/v1/cash-flow",
            "alerts": "/api/v1/alerts",
            "dashboard": "/api/v1/dashboard",
        },
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "finance.main:app",
        host=settings.host,
        port=settings.port,
        reload=True,
    )
