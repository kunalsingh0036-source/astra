"""
WhatsApp Gateway — A2A bridge.

Wraps the unified WhatsApp messaging gateway that handles all agent
WhatsApp communication: sending, receiving, conversation threading,
session windows, cooldowns, and template management.

Native API: FastAPI at /api/v1/
Port: 8600
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
)
from astra.a2a.server import A2AServer

AGENT_NAME = "whatsapp-gateway"
import os as _os

# Env-first; cloud default. The laptop-era localhost target was the
# reason every A2A task failed after the Railway migration — the
# bridge ran (eventually) but pointed at services that no longer
# listen on this machine.
BASE_URL = _os.environ.get("GATEWAY_URL", "").strip() or "http://whatsapp.railway.internal:8080"
BRIDGE_PORT = 8500

CARD = AgentCard(
    name=AGENT_NAME,
    description=(
        "Unified WhatsApp messaging gateway for the entire agent fleet. "
        "Single WhatsApp Business number shared by all agents. Handles "
        "outbound messaging (text, template, media), inbound webhook processing, "
        "conversation threading, 24hr session window enforcement, cross-agent "
        "cooldowns, contact management, and AI-powered reply classification."
    ),
    url=f"{_os.environ.get('A2A_BRIDGE_BASE', '').strip() or 'http://bridge.railway.internal:8500'}/whatsapp",
    version="0.1.0",
    capabilities=AgentCapabilities(
        streaming=False,
        push_notifications=True,
        cancellation=False,
    ),
    skills=[
        AgentSkill(
            id="send-message",
            name="Send WhatsApp Message",
            description=(
                "Send a WhatsApp message to a phone number via any agent. "
                "Supports text (session required), template (anytime), "
                "and media messages. Enforces cooldowns and session windows."
            ),
            tags=["whatsapp", "messaging", "outbound", "send"],
            input_schema={
                "type": "object",
                "properties": {
                    "agent_name": {"type": "string", "description": "Sending agent's name"},
                    "phone": {"type": "string", "description": "Recipient phone number"},
                    "message_type": {"type": "string", "enum": ["text", "template"]},
                    "content": {"type": "string", "description": "Text content (for text type)"},
                    "template_name": {"type": "string", "description": "Template name (for template type)"},
                },
                "required": ["agent_name", "phone", "message_type"],
            },
        ),
        AgentSkill(
            id="get-conversation",
            name="Get Conversation History",
            description=(
                "Retrieve conversations with optional filters by agent, phone, "
                "or status. Returns conversation list with message counts."
            ),
            tags=["whatsapp", "conversation", "history", "query"],
        ),
        AgentSkill(
            id="conversation-messages",
            name="Get Conversation Messages",
            description=(
                "Get full message history for a specific conversation. "
                "Returns all messages with direction, type, status, content."
            ),
            tags=["whatsapp", "messages", "history"],
        ),
        AgentSkill(
            id="list-templates",
            name="List WhatsApp Templates",
            description=(
                "List all Meta-approved WhatsApp message templates. "
                "Templates are required for initiating conversations outside "
                "the 24hr session window."
            ),
            tags=["whatsapp", "templates", "meta"],
        ),
        AgentSkill(
            id="gateway-stats",
            name="Gateway Statistics",
            description=(
                "Get WhatsApp gateway stats: total contacts, conversations, "
                "messages (outbound/inbound/failed counts)."
            ),
            tags=["whatsapp", "stats", "analytics", "dashboard"],
        ),
    ],
    model_tier="haiku",
    metadata={
        "project_path": "/Users/kunalsingh/Claude Code/whatsapp-gateway",
        "framework": "FastAPI",
        "database": "PostgreSQL (shared with Astra, port 5433)",
        "native_url": BASE_URL,
        "native_api_prefix": "/api/v1",
        "fleet_role": "communication_hub",
        "port": 8600,
    },
)


class WhatsAppGatewayBridge(A2AServer):
    """A2A bridge for the WhatsApp Gateway."""

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
        """Route A2A task to WhatsApp Gateway endpoints."""
        skill = params.skill_id or "gateway-stats"
        message_text = ""
        for part in params.message.parts:
            if part.type == "text" and isinstance(part.content, str):
                message_text += part.content

        try:
            client = await self._client()
            api = "/api/v1"

            if skill == "send-message":
                # Parse the message text as JSON, or build a basic payload
                try:
                    payload = json.loads(message_text)
                except (json.JSONDecodeError, TypeError):
                    payload = {
                        "agent_name": "astra",
                        "phone": message_text.strip(),
                        "message_type": "text",
                        "content": message_text,
                    }

                response = await client.post(f"{api}/send", json=payload)
                if response.status_code == 200:
                    data = response.json()
                    result_text = (
                        f"Message queued (ID: {data['message_id']}). "
                        f"Conversation: {data['conversation_id']}. "
                        f"Session open: {data['session_open']}"
                    )
                else:
                    result_text = f"Send failed ({response.status_code}): {response.text}"

            elif skill == "get-conversation":
                # Parse filters from message text
                params_dict = {}
                if message_text.strip():
                    # Support simple filter strings like "agent_name=helmtech"
                    for pair in message_text.strip().split("&"):
                        if "=" in pair:
                            k, v = pair.split("=", 1)
                            params_dict[k.strip()] = v.strip()

                response = await client.get(f"{api}/conversations", params=params_dict)
                data = response.json() if response.status_code == 200 else []
                count = len(data) if isinstance(data, list) else 0
                result_text = f"Found {count} conversations: {json.dumps(data, indent=2)}"

            elif skill == "conversation-messages":
                # Expect conversation ID in message
                conv_id = message_text.strip()
                response = await client.get(f"{api}/conversations/{conv_id}/messages")
                if response.status_code == 200:
                    data = response.json()
                    result_text = f"Messages ({len(data)}): {json.dumps(data, indent=2)}"
                else:
                    result_text = f"Error: {response.status_code} — {response.text}"

            elif skill == "list-templates":
                response = await client.get(f"{api}/templates/")
                data = response.json() if response.status_code == 200 else []
                result_text = f"Templates: {json.dumps(data, indent=2)}"

            elif skill == "gateway-stats":
                response = await client.get(f"{api}/stats")
                data = response.json() if response.status_code == 200 else {}
                result_text = f"WhatsApp Gateway stats: {json.dumps(data, indent=2)}"

            else:
                result_text = (
                    f"WhatsApp Gateway received: {message_text}. "
                    f"Skill '{skill}' not recognized."
                )

            task.complete(MessagePart(type="text", content=result_text))

        except httpx.ConnectError:
            task.fail(
                f"WhatsApp Gateway not running at {BASE_URL}. "
                f"Start it: cd whatsapp-gateway && uvicorn gateway.main:app --port 8600"
            )
        except Exception as e:
            task.fail(f"WhatsApp Gateway bridge error: {str(e)}")

        return task
