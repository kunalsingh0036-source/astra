"""
LinkedIn Agent — A2A bridge.

Wraps the FastAPI-based linkedin-agent at localhost:8002.
Capabilities: AI content generation, post scheduling, engagement
automation, brand management, profile analytics.

Native API: FastAPI at /api/
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

AGENT_NAME = "linkedin"
import os as _os

# Env-first; cloud default. The laptop-era localhost target was the
# reason every A2A task failed after the Railway migration — the
# bridge ran (eventually) but pointed at services that no longer
# listen on this machine.
BASE_URL = _os.environ.get("LINKEDIN_URL", "").strip() or "https://backend-production-a2994.up.railway.app"
BRIDGE_PORT = 8500

CARD = AgentCard(
    name=AGENT_NAME,
    description=(
        "Multi-account LinkedIn automation agent. AI-powered content generation, "
        "post scheduling, engagement automation (comments, likes, reposts), "
        "brand profile management, and performance analytics."
    ),
    url=f"{_os.environ.get('A2A_BRIDGE_BASE', '').strip() or 'http://bridge.railway.internal:8500'}/linkedin",
    version="0.1.0",
    capabilities=AgentCapabilities(
        streaming=False,
        push_notifications=False,
        cancellation=True,
    ),
    skills=[
        AgentSkill(
            id="generate-post",
            name="Generate LinkedIn Post",
            description=(
                "AI-generate a LinkedIn post based on brand profile, content "
                "pillars, and topic. Returns draft post content with optional "
                "image prompts."
            ),
            tags=["content", "linkedin", "post", "ai"],
            input_schema={
                "type": "object",
                "properties": {
                    "account_id": {"type": "string"},
                    "topic": {"type": "string"},
                    "content_type": {
                        "type": "string",
                        "enum": ["text", "image", "carousel", "video"],
                    },
                },
            },
        ),
        AgentSkill(
            id="schedule-post",
            name="Schedule Post",
            description=(
                "Schedule a post for a specific date/time or approve a draft "
                "for the next available slot."
            ),
            tags=["schedule", "publish", "linkedin"],
        ),
        AgentSkill(
            id="engagement-auto",
            name="Auto-Engage Feed",
            description=(
                "Discover relevant feed posts and generate comments or likes. "
                "Returns pending engagements for approval."
            ),
            tags=["engagement", "comments", "likes", "feed"],
        ),
        AgentSkill(
            id="content-calendar",
            name="Content Calendar",
            description=(
                "Get the content calendar view — scheduled, draft, and posted "
                "content for a date range."
            ),
            tags=["calendar", "schedule", "overview"],
        ),
        AgentSkill(
            id="brand-profile",
            name="Brand Profile",
            description=(
                "Get or update brand profile — positioning, content pillars, "
                "voice profile, target audience, visual identity."
            ),
            tags=["brand", "profile", "voice", "strategy"],
        ),
        AgentSkill(
            id="performance-insights",
            name="Performance Insights",
            description=(
                "Get AI-powered performance analysis — what content works, "
                "engagement trends, growth metrics."
            ),
            tags=["analytics", "insights", "performance"],
        ),
        AgentSkill(
            id="account-health",
            name="Account Health",
            description=(
                "Check LinkedIn account session status, extension connection, "
                "posting schedule compliance."
            ),
            tags=["health", "status", "accounts"],
        ),
        AgentSkill(
            id="reply-management",
            name="Reply Management",
            description=(
                "Check for new comments on posts, generate AI reply suggestions, "
                "approve and send replies."
            ),
            tags=["replies", "comments", "engagement"],
        ),
    ],
    model_tier="sonnet",
    metadata={
        "project_path": "/Users/kunalsingh/Claude Code/linkedin-agent",
        "framework": "FastAPI + Next.js",
        "database": "PostgreSQL 16",
        "native_url": BASE_URL,
        "native_api_prefix": "/api",
    },
)


class LinkedInBridge(A2AServer):
    """A2A bridge for the LinkedIn Agent."""

    def __init__(self):
        super().__init__(card=CARD)
        self._http: httpx.AsyncClient | None = None

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                headers={"x-astra-secret": _os.environ.get("AGENT_SHARED_SECRET", "").strip()},
                base_url=BASE_URL,
                timeout=30.0,
            )
        return self._http

    async def handle_task(self, task: Task, params: TaskSendParams) -> Task:
        """Route A2A task to the appropriate FastAPI endpoint."""
        skill = params.skill_id or "performance-insights"
        message_text = ""
        for part in params.message.parts:
            if part.type == "text" and isinstance(part.content, str):
                message_text += part.content

        try:
            client = await self._client()

            if skill == "generate-post":
                response = await client.post(
                    "/api/content/generate",
                    json={"prompt": message_text},
                )
                data = response.json() if response.status_code == 200 else {}
                result_text = (
                    f"Post generation {'succeeded' if response.status_code == 200 else 'failed'} "
                    f"(status={response.status_code}).\n"
                    f"Response: {data}"
                )

            elif skill == "content-calendar":
                response = await client.get("/api/content/calendar/view")
                data = response.json() if response.status_code == 200 else {}
                result_text = f"Content calendar: {data}"

            elif skill == "engagement-auto":
                response = await client.post("/api/engagement/auto-comment")
                data = response.json() if response.status_code == 200 else {}
                result_text = f"Auto-engagement triggered: {data}"

            elif skill == "performance-insights":
                response = await client.get(
                    "/api/content/insights/performance"
                )
                data = response.json() if response.status_code == 200 else {}
                result_text = f"Performance insights: {data}"

            elif skill == "account-health":
                response = await client.get("/api/accounts")
                data = response.json() if response.status_code == 200 else {}
                result_text = f"Accounts status: {data}"

            elif skill == "reply-management":
                response = await client.post(
                    "/api/engagement/replies/check"
                )
                data = response.json() if response.status_code == 200 else {}
                result_text = f"Reply check: {data}"

            elif skill == "brand-profile":
                # Need account_id from message context
                result_text = (
                    f"Brand profile request acknowledged. "
                    f"Specify account_id for full retrieval. Task: {message_text}"
                )

            else:
                result_text = (
                    f"LinkedIn agent received: {message_text}. "
                    f"Skill '{skill}' processing."
                )

            task.complete(MessagePart(type="text", content=result_text))

        except httpx.ConnectError:
            task.fail(
                f"LinkedIn agent not running at {BASE_URL}. "
                f"Start: cd linkedin-agent/backend && uvicorn main:app --port 8000"
            )
        except Exception as e:
            task.fail(f"LinkedIn bridge error: {str(e)}")

        return task
