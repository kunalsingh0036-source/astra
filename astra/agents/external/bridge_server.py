"""
A2A Bridge Server — runs all external agent bridges on a single FastAPI app.

Instead of 4 separate processes, this mounts all bridge routers on one
FastAPI instance with path-based routing:

    /bookkeeper/a2a/...        → Bookkeeper bridge
    /linkedin/a2a/...          → LinkedIn bridge
    /helmtech/a2a/...          → HelmTech bridge
    /apex/a2a/...              → Apex bridge

Each bridge also gets its own /.well-known/agent.json endpoint.

Run with:
    python -m astra.agents.external.bridge_server

Or from the astra CLI:
    astra-bridges
"""

import uvicorn
from fastapi import FastAPI

from astra.agents.external.bookkeeper import BookkeeperBridge
from astra.agents.external.linkedin import LinkedInBridge
from astra.agents.external.helmtech import HelmTechBridge
from astra.agents.external.apex import ApexBridge
from astra.agents.external.whatsapp import WhatsAppGatewayBridge
from astra.agents.external.finance import FinanceBridge
from astra.agents.external.email_agent import EmailAgentBridge

BRIDGE_PORT = 8500


def create_bridge_app() -> FastAPI:
    """Create the unified bridge server with all 7 agent bridges."""

    app = FastAPI(
        title="Astra A2A Bridge Server",
        description=(
            "Unified A2A protocol bridge for all external agents in "
            "Kunal's fleet. Each agent is mounted at /{agent_name}/."
        ),
        version="0.1.0",
    )

    # Initialize bridges
    bookkeeper = BookkeeperBridge()
    linkedin = LinkedInBridge()
    helmtech = HelmTechBridge()
    apex = ApexBridge()
    whatsapp = WhatsAppGatewayBridge()
    finance = FinanceBridge()
    email = EmailAgentBridge()

    # Mount each bridge under its own prefix
    # The A2A endpoints become: /{name}/a2a/tasks, /{name}/a2a/health, etc.
    app.include_router(bookkeeper.router, prefix="/bookkeeper")
    app.include_router(bookkeeper.well_known_router, prefix="/bookkeeper")

    app.include_router(linkedin.router, prefix="/linkedin")
    app.include_router(linkedin.well_known_router, prefix="/linkedin")

    app.include_router(helmtech.router, prefix="/helmtech")
    app.include_router(helmtech.well_known_router, prefix="/helmtech")

    app.include_router(apex.router, prefix="/apex")
    app.include_router(apex.well_known_router, prefix="/apex")

    app.include_router(whatsapp.router, prefix="/whatsapp")
    app.include_router(whatsapp.well_known_router, prefix="/whatsapp")

    app.include_router(finance.router, prefix="/finance")
    app.include_router(finance.well_known_router, prefix="/finance")

    app.include_router(email.router, prefix="/email")
    app.include_router(email.well_known_router, prefix="/email")

    # Root health check for the bridge server itself. Keeps the agent
    # names consistent with the route mounts (`/{name}/a2a/tasks`) so
    # the /health response is a truthful index rather than a parallel
    # naming scheme.
    @app.get("/health")
    async def bridge_health():
        return {
            "status": "healthy",
            "service": "astra-a2a-bridge-server",
            "agents": ["bookkeeper", "linkedin", "helmtech", "apex", "whatsapp", "finance", "email"],
            "port": BRIDGE_PORT,
        }

    # Root endpoint listing all mounted agents
    @app.get("/")
    async def root():
        return {
            "service": "Astra A2A Bridge Server",
            "agents": {
                "bookkeeper": {
                    "card": f"http://localhost:{BRIDGE_PORT}/bookkeeper/.well-known/agent.json",
                    "health": f"http://localhost:{BRIDGE_PORT}/bookkeeper/a2a/health",
                    "tasks": f"http://localhost:{BRIDGE_PORT}/bookkeeper/a2a/tasks",
                },
                "linkedin": {
                    "card": f"http://localhost:{BRIDGE_PORT}/linkedin/.well-known/agent.json",
                    "health": f"http://localhost:{BRIDGE_PORT}/linkedin/a2a/health",
                    "tasks": f"http://localhost:{BRIDGE_PORT}/linkedin/a2a/tasks",
                },
                "helmtech": {
                    "card": f"http://localhost:{BRIDGE_PORT}/helmtech/.well-known/agent.json",
                    "health": f"http://localhost:{BRIDGE_PORT}/helmtech/a2a/health",
                    "tasks": f"http://localhost:{BRIDGE_PORT}/helmtech/a2a/tasks",
                },
                "apex": {
                    "card": f"http://localhost:{BRIDGE_PORT}/apex/.well-known/agent.json",
                    "health": f"http://localhost:{BRIDGE_PORT}/apex/a2a/health",
                    "tasks": f"http://localhost:{BRIDGE_PORT}/apex/a2a/tasks",
                },
                "whatsapp": {
                    "card": f"http://localhost:{BRIDGE_PORT}/whatsapp/.well-known/agent.json",
                    "health": f"http://localhost:{BRIDGE_PORT}/whatsapp/a2a/health",
                    "tasks": f"http://localhost:{BRIDGE_PORT}/whatsapp/a2a/tasks",
                },
                "finance": {
                    "card": f"http://localhost:{BRIDGE_PORT}/finance/.well-known/agent.json",
                    "health": f"http://localhost:{BRIDGE_PORT}/finance/a2a/health",
                    "tasks": f"http://localhost:{BRIDGE_PORT}/finance/a2a/tasks",
                },
                "email": {
                    "card": f"http://localhost:{BRIDGE_PORT}/email/.well-known/agent.json",
                    "health": f"http://localhost:{BRIDGE_PORT}/email/a2a/health",
                    "tasks": f"http://localhost:{BRIDGE_PORT}/email/a2a/tasks",
                },
            },
        }

    return app


app = create_bridge_app()


def main():
    """Run the bridge server."""
    print(f"\n🌉 Astra A2A Bridge Server starting on port {BRIDGE_PORT}")
    print(f"   Bookkeeper: http://localhost:{BRIDGE_PORT}/bookkeeper/a2a/health")
    print(f"   LinkedIn:   http://localhost:{BRIDGE_PORT}/linkedin/a2a/health")
    print(f"   HelmTech:   http://localhost:{BRIDGE_PORT}/helmtech/a2a/health")
    print(f"   Apex:       http://localhost:{BRIDGE_PORT}/apex/a2a/health")
    print(f"   WhatsApp:   http://localhost:{BRIDGE_PORT}/whatsapp/a2a/health")
    print(f"   Finance:    http://localhost:{BRIDGE_PORT}/finance/a2a/health")
    print(f"   Email:      http://localhost:{BRIDGE_PORT}/email/a2a/health")
    print(f"   Docs:       http://localhost:{BRIDGE_PORT}/docs")
    print()
    uvicorn.run(app, host="0.0.0.0", port=BRIDGE_PORT)


if __name__ == "__main__":
    main()
