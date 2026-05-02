"""
HelmTech Outreach Agent — A2A bridge.

Wraps the FastAPI-based helmtech-outreach-agent.
Capabilities: B2B lead discovery for Indian SMBs, lead enrichment,
scoring, multi-channel outreach campaigns, autopilot, analytics.

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

AGENT_NAME = "helmtech-outreach"
BASE_URL = "http://localhost:8003"
BRIDGE_PORT = 8500

CARD = AgentCard(
    name=AGENT_NAME,
    description=(
        "B2B sales automation agent for HelmTech. Discovers Indian SMBs without "
        "websites, enriches leads via Apollo/Hunter, scores them, and runs "
        "multi-channel outreach campaigns (email, WhatsApp, LinkedIn, Instagram). "
        "Full autopilot mode available."
    ),
    url=f"http://localhost:{BRIDGE_PORT}/helmtech",
    version="0.1.0",
    capabilities=AgentCapabilities(
        streaming=False,
        push_notifications=False,
        cancellation=True,
    ),
    skills=[
        AgentSkill(
            id="lead-discovery",
            name="Discover Leads",
            description=(
                "Search Apollo.io, Google Maps, JustDial, and Serper.dev for "
                "Indian SMBs matching the ICP. Returns discovered leads."
            ),
            tags=["leads", "discovery", "apollo", "search"],
            input_schema={
                "type": "object",
                "properties": {
                    "job_titles": {"type": "array", "items": {"type": "string"}},
                    "industries": {"type": "array", "items": {"type": "string"}},
                    "locations": {"type": "array", "items": {"type": "string"}},
                },
            },
        ),
        AgentSkill(
            id="lead-enrichment",
            name="Enrich Leads",
            description=(
                "Enrich leads with company data, email verification, LinkedIn "
                "profiles, and company signals via Apollo/Hunter/ProxyCurl."
            ),
            tags=["enrichment", "data", "leads"],
        ),
        AgentSkill(
            id="lead-scoring",
            name="Score Leads",
            description=(
                "AI-powered lead scoring based on company signals, industry fit, "
                "online maturity, and engagement history. Returns score 0-100."
            ),
            tags=["scoring", "ai", "qualification"],
        ),
        AgentSkill(
            id="campaign-management",
            name="Campaign Management",
            description=(
                "Create, launch, pause, or monitor outreach campaigns. "
                "Shows campaign status, enrollment counts, and performance."
            ),
            tags=["campaigns", "outreach", "management"],
        ),
        AgentSkill(
            id="message-generation",
            name="Generate Outreach Message",
            description=(
                "AI-generate a personalized outreach message for a lead "
                "across any channel (email, WhatsApp, LinkedIn, Instagram)."
            ),
            tags=["messages", "ai", "personalization"],
        ),
        AgentSkill(
            id="autopilot-control",
            name="Autopilot Control",
            description=(
                "Enable, disable, or check status of the fully autonomous "
                "pipeline: discover → enrich → score → sequence → campaign."
            ),
            tags=["autopilot", "automation", "control"],
        ),
        AgentSkill(
            id="pipeline-status",
            name="Pipeline Status",
            description=(
                "Get dashboard stats: total leads, pipeline breakdown, active "
                "campaigns, message counts, response rates."
            ),
            tags=["dashboard", "stats", "overview"],
        ),
        AgentSkill(
            id="analytics",
            name="Outreach Analytics",
            description=(
                "Get analytics: daily trends, channel performance, funnel, "
                "A/B test results, AI-powered trend insights."
            ),
            tags=["analytics", "metrics", "insights"],
        ),
        AgentSkill(
            id="response-handling",
            name="Response Intelligence",
            description=(
                "Classify inbound messages (interested, meeting request, spam), "
                "suggest AI replies, show pending responses needing attention."
            ),
            tags=["responses", "classification", "replies"],
        ),
    ],
    model_tier="sonnet",
    metadata={
        "project_path": "/Users/kunalsingh/Claude Code/helmtech-outreach-agent",
        "framework": "FastAPI",
        "database": "SQLite (dev)",
        "native_url": BASE_URL,
        "native_api_prefix": "/api/v1",
        "company": "HelmTech",
    },
)


class HelmTechBridge(A2AServer):
    """A2A bridge for the HelmTech Outreach Agent."""

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
        """Route A2A task to the HelmTech FastAPI endpoints."""
        skill = params.skill_id or "pipeline-status"
        message_text = ""
        for part in params.message.parts:
            if part.type == "text" and isinstance(part.content, str):
                message_text += part.content

        try:
            client = await self._client()
            api = "/api/v1"

            if skill == "pipeline-status":
                response = await client.get(f"{api}/dashboard/stats")
                data = response.json() if response.status_code == 200 else {}
                result_text = f"HelmTech pipeline status: {data}"

            elif skill == "lead-discovery":
                response = await client.post(
                    f"{api}/discovery/search/people",
                    json={"query": message_text},
                )
                data = response.json() if response.status_code == 200 else {}
                result_text = f"Discovery results: {data}"

            elif skill == "lead-scoring":
                response = await client.post(f"{api}/discovery/score/batch")
                data = response.json() if response.status_code == 200 else {}
                result_text = f"Batch scoring triggered: {data}"

            elif skill == "campaign-management":
                response = await client.get(f"{api}/campaigns")
                data = response.json() if response.status_code == 200 else {}
                result_text = f"Campaigns: {data}"

            elif skill == "autopilot-control":
                response = await client.get(f"{api}/automation/status")
                data = response.json() if response.status_code == 200 else {}
                result_text = f"Autopilot status: {data}"

            elif skill == "analytics":
                response = await client.get(f"{api}/analytics/overview")
                data = response.json() if response.status_code == 200 else {}
                result_text = f"HelmTech analytics: {data}"

            elif skill == "message-generation":
                response = await client.post(
                    f"{api}/messages/generate",
                    json={"prompt": message_text},
                )
                data = response.json() if response.status_code == 200 else {}
                result_text = f"Generated message: {data}"

            elif skill == "response-handling":
                response = await client.get(f"{api}/messages/pending-replies")
                data = response.json() if response.status_code == 200 else {}
                result_text = f"Pending replies: {data}"

            else:
                result_text = (
                    f"HelmTech agent received: {message_text}. "
                    f"Skill '{skill}' acknowledged."
                )

            task.complete(MessagePart(type="text", content=result_text))

        except httpx.ConnectError:
            task.fail(
                f"HelmTech agent not running at {BASE_URL}. "
                f"Start: cd helmtech-outreach-agent && docker-compose up"
            )
        except Exception as e:
            task.fail(f"HelmTech bridge error: {str(e)}")

        return task
