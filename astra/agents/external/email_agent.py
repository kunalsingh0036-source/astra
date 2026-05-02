"""
Email Agent — A2A bridge.

AI-powered email management: Gmail sync, AI classification, smart drafting,
scheduled sending, contact management, and template-based composition.

Native API: FastAPI at /api/v1/
Port: 8005
"""

import json
import httpx

from astra.a2a.models import (
    AgentCard,
    AgentCapabilities,
    AgentSkill,
    MessagePart,
    Task,
    TaskSendParams,
    TaskState,
)
from astra.a2a.server import A2AServer

AGENT_NAME = "email-agent"
BASE_URL = "http://localhost:8005"
BRIDGE_PORT = 8500

CARD = AgentCard(
    name=AGENT_NAME,
    description=(
        "AI-powered email management agent. Syncs with Gmail, classifies emails "
        "by category/priority/action needed, generates AI drafts with Claude Sonnet, "
        "manages contacts, templates, and scheduled sending. Handles all of Kunal's "
        "email operations across personal and business accounts."
    ),
    url=f"http://localhost:{BRIDGE_PORT}/email",
    version="0.1.0",
    capabilities=AgentCapabilities(
        streaming=False,
        push_notifications=False,
        cancellation=False,
    ),
    skills=[
        AgentSkill(
            id="inbox-summary",
            name="Inbox Summary",
            description=(
                "Get inbox summary: total messages, unread count, action-needed count, "
                "breakdown by category. Optionally filter by account_id."
            ),
        ),
        AgentSkill(
            id="list-messages",
            name="List Messages",
            description=(
                "List emails with filters: account_id, direction (inbound/outbound), "
                "category, unread_only, action_needed_only."
            ),
        ),
        AgentSkill(
            id="list-threads",
            name="List Threads",
            description=(
                "List email threads with filters: priority (urgent/high/normal/low), "
                "needs_response, category."
            ),
        ),
        AgentSkill(
            id="send-email",
            name="Send Email",
            description=(
                "Send an email via Gmail. Required: to (list), subject, body. "
                "Optional: cc, bcc."
            ),
        ),
        AgentSkill(
            id="generate-draft",
            name="Generate AI Draft",
            description=(
                "Generate an email draft using Claude Sonnet. Provide: account_id, "
                "to (list), intent (what to say), tone (professional/casual/friendly/firm). "
                "Optional: subject, reply_to_message_id, template_id."
            ),
        ),
        AgentSkill(
            id="classify-email",
            name="Classify Email",
            description=(
                "AI-classify an email: category, priority, summary, action_needed. "
                "Provide from_address, to_addresses, subject, body_text."
            ),
        ),
        AgentSkill(
            id="list-contacts",
            name="List Contacts",
            description="List email contacts. Optionally filter by category (client/vendor/team/personal).",
        ),
        AgentSkill(
            id="list-templates",
            name="List Email Templates",
            description="List available email templates. Optionally filter by category.",
        ),
    ],
    model_tier="sonnet",
)


class EmailAgentBridge(A2AServer):
    """A2A bridge that routes tasks to the Email Agent REST API."""

    def __init__(self):
        super().__init__(card=CARD)

    async def handle_task(self, task: Task, params: TaskSendParams) -> Task:
        skill_id = params.skill_id
        payload = {}
        for part in params.message.parts:
            if part.type == "json" and isinstance(part.content, dict):
                payload = part.content
            elif part.type == "text" and isinstance(part.content, str):
                try:
                    payload = json.loads(part.content)
                except json.JSONDecodeError:
                    payload = {"query": part.content}

        try:
            async with httpx.AsyncClient(base_url=BASE_URL, timeout=30) as client:
                result = await self._route(client, skill_id, payload)

            task.state = TaskState.COMPLETED
            task.result = MessagePart(
                type="text",
                content=json.dumps(result, indent=2, default=str),
            )
        except Exception as e:
            task.state = TaskState.FAILED
            task.error = str(e)

        return task

    async def _route(self, client: httpx.AsyncClient, skill_id: str | None, payload: dict) -> dict:
        if skill_id == "inbox-summary":
            params = {}
            if "account_id" in payload:
                params["account_id"] = payload["account_id"]
            r = await client.get("/api/v1/messages/summary", params=params)
            r.raise_for_status()
            return r.json()

        elif skill_id == "list-messages":
            params = {k: v for k, v in payload.items()
                      if k in ("account_id", "direction", "category", "unread_only", "action_needed_only", "limit")}
            r = await client.get("/api/v1/messages/", params=params)
            r.raise_for_status()
            return {"messages": r.json(), "count": len(r.json())}

        elif skill_id == "list-threads":
            params = {k: v for k, v in payload.items()
                      if k in ("account_id", "priority", "needs_response", "category", "limit")}
            r = await client.get("/api/v1/threads/", params=params)
            r.raise_for_status()
            return {"threads": r.json(), "count": len(r.json())}

        elif skill_id == "send-email":
            r = await client.post("/api/v1/messages/send", json=payload)
            r.raise_for_status()
            return r.json()

        elif skill_id == "generate-draft":
            r = await client.post("/api/v1/drafts/generate", json=payload)
            r.raise_for_status()
            return r.json()

        elif skill_id == "classify-email":
            r = await client.post("/api/v1/ai/classify", json=payload)
            r.raise_for_status()
            return r.json()

        elif skill_id == "list-contacts":
            params = {k: v for k, v in payload.items() if k in ("category", "limit")}
            r = await client.get("/api/v1/contacts/", params=params)
            r.raise_for_status()
            return {"contacts": r.json(), "count": len(r.json())}

        elif skill_id == "list-templates":
            params = {k: v for k, v in payload.items() if k in ("category",)}
            r = await client.get("/api/v1/templates/", params=params)
            r.raise_for_status()
            return {"templates": r.json(), "count": len(r.json())}

        else:
            return {"error": f"Unknown skill: {skill_id}", "available_skills": [s.id for s in CARD.skills]}
