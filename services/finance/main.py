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
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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
