"""
A2A agent definition for the WhatsApp Gateway.

Exposes the gateway as an A2A-compatible agent that Astra can discover,
send tasks to, and get results from.

Skills:
- send-message: Send a WhatsApp message through the gateway
- get-conversation: Retrieve conversation history
- check-session: Check if a contact's session window is open
- list-templates: List available templates
- gateway-stats: Get gateway statistics
"""

import sys
import os

# Add Astra to path for A2A imports
ASTRA_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "astra"
)
if os.path.exists(ASTRA_PATH):
    sys.path.insert(0, os.path.dirname(ASTRA_PATH))

from astra.a2a.models import (
    AgentCard,
    AgentCapabilities,
    AgentSkill,
    MessagePart,
    Task,
    TaskSendParams,
)
from astra.a2a.server import A2AServer


def _get_agent_card() -> AgentCard:
    """Create the Agent Card for the WhatsApp Gateway."""
    return AgentCard(
        name="whatsapp-gateway",
        description=(
            "Unified WhatsApp messaging gateway for all agents. "
            "Handles sending, receiving, conversation threading, "
            "session windows, cooldowns, and template management."
        ),
        url="http://localhost:8600",
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
                    "Send a WhatsApp message to a phone number. "
                    "Supports text, template, and media messages. "
                    "Handles session windows and cooldowns automatically."
                ),
                tags=["whatsapp", "messaging", "outbound"],
            ),
            AgentSkill(
                id="get-conversation",
                name="Get Conversation",
                description="Retrieve conversation history for a phone number or conversation ID.",
                tags=["whatsapp", "conversation", "history"],
            ),
            AgentSkill(
                id="check-session",
                name="Check Session Window",
                description=(
                    "Check if a contact's 24hr session window is open. "
                    "Returns whether free-form text or only templates are allowed."
                ),
                tags=["whatsapp", "session"],
            ),
            AgentSkill(
                id="list-templates",
                name="List Templates",
                description="List available Meta-approved WhatsApp message templates.",
                tags=["whatsapp", "templates"],
            ),
            AgentSkill(
                id="gateway-stats",
                name="Gateway Statistics",
                description="Get WhatsApp gateway stats: contacts, conversations, messages sent/received.",
                tags=["whatsapp", "stats"],
            ),
        ],
        model_tier="haiku",
        metadata={"port": 8600, "fleet_role": "communication_hub"},
    )


class WhatsAppGatewayAgent(A2AServer):
    """A2A server for the WhatsApp Gateway."""

    async def handle_task(self, task: Task, params: TaskSendParams) -> Task:
        """Route A2A tasks to the appropriate gateway function."""
        skill_id = params.skill_id or "gateway-stats"

        # Extract the text instruction from the message
        instruction = ""
        for part in params.message.parts:
            if part.type == "text" and isinstance(part.content, str):
                instruction += part.content

        try:
            if skill_id == "send-message":
                result_text = await self._handle_send(instruction)
            elif skill_id == "get-conversation":
                result_text = await self._handle_get_conversation(instruction)
            elif skill_id == "check-session":
                result_text = await self._handle_check_session(instruction)
            elif skill_id == "list-templates":
                result_text = await self._handle_list_templates()
            elif skill_id == "gateway-stats":
                result_text = await self._handle_stats()
            else:
                result_text = f"Unknown skill: {skill_id}"

            task.complete(MessagePart(type="text", content=result_text))

        except Exception as e:
            task.fail(str(e))

        return task

    async def _handle_send(self, instruction: str) -> str:
        """Handle send-message skill via the REST API internally."""
        # In production, this would call the send endpoint logic directly
        # For now, return instructions
        return (
            "To send a message, use POST http://localhost:8600/api/v1/send "
            "with: {agent_name, phone, message_type, content/template_name}"
        )

    async def _handle_get_conversation(self, instruction: str) -> str:
        """Handle get-conversation skill."""
        return (
            "To get conversations, use GET http://localhost:8600/api/v1/conversations "
            "with optional filters: ?agent_name=X&phone=Y&status=Z"
        )

    async def _handle_check_session(self, instruction: str) -> str:
        """Handle check-session skill."""
        return (
            "Session window check: send GET to conversations endpoint "
            "and check the session_open field."
        )

    async def _handle_list_templates(self) -> str:
        """Handle list-templates skill."""
        return "Use GET http://localhost:8600/api/v1/templates/ to list all templates."

    async def _handle_stats(self) -> str:
        """Handle gateway-stats skill."""
        return "Use GET http://localhost:8600/api/v1/stats for gateway statistics."


# Create singleton instances
agent_card = _get_agent_card()
whatsapp_agent = WhatsAppGatewayAgent(card=agent_card)
