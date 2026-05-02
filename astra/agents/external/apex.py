"""
Apex Outreach Agent — A2A bridge.

Wraps the FastAPI-based apex-outreach-agent at localhost:8001.
Capabilities: Lead management, multi-channel outreach (email, LinkedIn,
WhatsApp, Instagram), CRM (clients, orders, quotes), autopilot, analytics.

Native API: FastAPI at /api/v1/
"""

import httpx

from astra.a2a.models import (
    AgentCard,
    AgentCapabilities,
    AgentSkill,
    MessagePart,
    Task,
    TaskSendParams,
)
from astra.a2a.server import A2AServer

AGENT_NAME = "apex-outreach"
BASE_URL = "http://localhost:8001"
BRIDGE_PORT = 8500

CARD = AgentCard(
    name=AGENT_NAME,
    description=(
        "Apex Human's AI-powered B2B outreach and CRM agent. Manages leads, "
        "runs multi-channel outreach campaigns (email, LinkedIn, WhatsApp, "
        "Instagram), tracks orders through 7-stage pipeline, and provides "
        "full CRM with client management, quotes, and products."
    ),
    url=f"http://localhost:{BRIDGE_PORT}/apex",
    version="0.1.0",
    capabilities=AgentCapabilities(
        streaming=False,
        push_notifications=False,
        cancellation=True,
    ),
    skills=[
        AgentSkill(
            id="lead-management",
            name="Lead Management",
            description=(
                "Create, update, list, and move leads through pipeline stages "
                "(prospect → qualified → negotiation → client → lost)."
            ),
            tags=["leads", "pipeline", "crm"],
        ),
        AgentSkill(
            id="lead-discovery",
            name="Lead Discovery & Enrichment",
            description=(
                "Search Apollo.io/Hunter.io for prospects, verify emails, "
                "enrich lead data with company and LinkedIn info."
            ),
            tags=["discovery", "enrichment", "apollo", "hunter"],
        ),
        AgentSkill(
            id="campaign-ops",
            name="Campaign Operations",
            description=(
                "Create, launch, pause, monitor outreach campaigns. "
                "Manage sequences and enrollments."
            ),
            tags=["campaigns", "sequences", "outreach"],
        ),
        AgentSkill(
            id="message-ops",
            name="Message Operations",
            description=(
                "Send messages across channels (email/LinkedIn/WhatsApp/Instagram), "
                "generate AI messages, classify inbound responses."
            ),
            tags=["messages", "email", "whatsapp", "linkedin"],
        ),
        AgentSkill(
            id="autopilot",
            name="Autopilot Control",
            description=(
                "Full autonomous pipeline: discover → enrich → score → create "
                "sequences → launch campaigns. Toggle on/off, configure ICP."
            ),
            tags=["autopilot", "automation"],
        ),
        AgentSkill(
            id="client-management",
            name="Client Management",
            description=(
                "Manage paying clients: create clients from leads, log "
                "interactions, track AMA tier and renewal dates."
            ),
            tags=["clients", "crm", "relationships"],
        ),
        AgentSkill(
            id="order-pipeline",
            name="Order Pipeline",
            description=(
                "7-stage order tracking: brief → design → tech_spec → sampling "
                "→ production → QC → delivery. With financials and line items."
            ),
            tags=["orders", "pipeline", "fulfillment"],
        ),
        AgentSkill(
            id="quotes",
            name="Quote Management",
            description=(
                "Create, update, and track sales quotes with line items, "
                "validity dates, and conversion tracking."
            ),
            tags=["quotes", "proposals", "sales"],
        ),
        AgentSkill(
            id="dashboard",
            name="Dashboard & Analytics",
            description=(
                "Overview stats, daily trends, channel performance, funnel "
                "analysis, campaign metrics, AI-powered insights."
            ),
            tags=["analytics", "dashboard", "metrics"],
        ),
        AgentSkill(
            id="revenue",
            name="Revenue Dashboard",
            description=(
                "Revenue metrics, order values, client lifetime value, "
                "pipeline forecast."
            ),
            tags=["revenue", "financials", "forecast"],
        ),
    ],
    model_tier="sonnet",
    metadata={
        "project_path": "/Users/kunalsingh/Claude Code/apex-outreach-agent",
        "framework": "FastAPI + Next.js",
        "database": "PostgreSQL 16",
        "native_url": BASE_URL,
        "native_api_prefix": "/api/v1",
        "company": "Apex Human",
    },
)


class ApexBridge(A2AServer):
    """A2A bridge for the Apex Outreach Agent."""

    def __init__(self):
        super().__init__(card=CARD)
        self._http: httpx.AsyncClient | None = None

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                base_url=BASE_URL,
                timeout=30.0,
            )
        return self._http

    async def handle_task(self, task: Task, params: TaskSendParams) -> Task:
        """Route A2A task to the Apex FastAPI endpoints."""
        skill = params.skill_id or "dashboard"
        message_text = ""
        for part in params.message.parts:
            if part.type == "text" and isinstance(part.content, str):
                message_text += part.content

        try:
            client = await self._client()
            api = "/api/v1"

            if skill == "dashboard":
                response = await client.get(f"{api}/dashboard/stats")
                data = response.json() if response.status_code == 200 else {}
                result_text = f"Apex dashboard: {data}"

            elif skill == "lead-management":
                response = await client.get(f"{api}/leads")
                data = response.json() if response.status_code == 200 else {}
                result_text = f"Leads: {data}"

            elif skill == "lead-discovery":
                response = await client.post(
                    f"{api}/discovery/search/people",
                    json={"query": message_text},
                )
                data = response.json() if response.status_code == 200 else {}
                result_text = f"Discovery: {data}"

            elif skill == "campaign-ops":
                response = await client.get(f"{api}/campaigns")
                data = response.json() if response.status_code == 200 else {}
                result_text = f"Campaigns: {data}"

            elif skill == "message-ops":
                response = await client.post(
                    f"{api}/messages/generate",
                    json={"prompt": message_text},
                )
                data = response.json() if response.status_code == 200 else {}
                result_text = f"Generated message: {data}"

            elif skill == "autopilot":
                response = await client.get(f"{api}/automation/status")
                data = response.json() if response.status_code == 200 else {}
                result_text = f"Apex autopilot: {data}"

            elif skill == "client-management":
                response = await client.get(f"{api}/clients")
                data = response.json() if response.status_code == 200 else {}
                result_text = f"Clients: {data}"

            elif skill == "order-pipeline":
                response = await client.get(f"{api}/orders")
                data = response.json() if response.status_code == 200 else {}
                result_text = f"Orders: {data}"

            elif skill == "quotes":
                response = await client.get(f"{api}/quotes")
                data = response.json() if response.status_code == 200 else {}
                result_text = f"Quotes: {data}"

            elif skill == "revenue":
                response = await client.get(f"{api}/revenue")
                data = response.json() if response.status_code == 200 else {}
                result_text = f"Revenue: {data}"

            else:
                result_text = (
                    f"Apex agent received: {message_text}. "
                    f"Skill '{skill}' acknowledged."
                )

            task.complete(MessagePart(type="text", content=result_text))

        except httpx.ConnectError:
            task.fail(
                f"Apex agent not running at {BASE_URL}. "
                f"Start: cd apex-outreach-agent && ./dev.sh start"
            )
        except Exception as e:
            task.fail(f"Apex bridge error: {str(e)}")

        return task
