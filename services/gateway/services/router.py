"""
Inbound message router — decides which agent handles an incoming message.

Routing priority:
1. Existing conversation with an owning agent → route there
2. Contact was created by an agent → route to that agent
3. Claim rules match (phone prefix, keywords) → route to matching agent
4. Default → hold as pending, notify Astra
"""

import logging
from dataclasses import dataclass

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.models.agent_registration import AgentRegistration
from gateway.models.contact import Contact
from gateway.models.conversation import Conversation, ConversationStatus

logger = logging.getLogger(__name__)


@dataclass
class RoutingDecision:
    agent_name: str | None
    confidence: float
    action: str  # "forward", "hold", "auto_reply"
    reason: str


class InboundRouter:
    """Routes inbound WhatsApp messages to the right agent."""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def route(
        self, contact_id, message_text: str
    ) -> RoutingDecision:
        """Determine which agent should handle this inbound message.

        Args:
            contact_id: The contact UUID
            message_text: The inbound message text

        Returns:
            RoutingDecision with agent_name and action
        """
        # 1. Check for active conversation with an owner
        result = await self._session.execute(
            select(Conversation).where(
                Conversation.contact_id == contact_id,
                Conversation.status == ConversationStatus.ACTIVE,
                Conversation.owning_agent.isnot(None),
            ).order_by(Conversation.last_message_at.desc())
        )
        active_conv = result.scalar_one_or_none()

        if active_conv:
            return RoutingDecision(
                agent_name=active_conv.owning_agent,
                confidence=0.95,
                action="forward",
                reason=f"Active conversation owned by {active_conv.owning_agent}",
            )

        # 2. Check source agent of the contact
        contact = await self._session.get(Contact, contact_id)
        if contact and contact.source_agent:
            return RoutingDecision(
                agent_name=contact.source_agent,
                confidence=0.80,
                action="forward",
                reason=f"Contact created by {contact.source_agent}",
            )

        # 3. Check claim rules from registered agents
        registrations = await self._session.execute(
            select(AgentRegistration).where(
                AgentRegistration.is_active == True
            ).order_by(AgentRegistration.priority.desc())
        )
        for reg in registrations.scalars().all():
            if self._matches_claim_rules(reg, contact, message_text):
                return RoutingDecision(
                    agent_name=reg.name,
                    confidence=0.70,
                    action="forward",
                    reason=f"Claim rules matched for {reg.name}",
                )

        # 4. No match — hold for review
        return RoutingDecision(
            agent_name=None,
            confidence=0.0,
            action="hold",
            reason="No agent matched. Message held as pending.",
        )

    def _matches_claim_rules(
        self, reg: AgentRegistration, contact: Contact | None, message_text: str
    ) -> bool:
        """Check if a registration's claim rules match this message."""
        rules = reg.claim_rules or {}

        # Phone prefix match
        prefixes = rules.get("phone_prefixes", [])
        if prefixes and contact:
            for prefix in prefixes:
                if contact.phone.startswith(prefix):
                    return True

        # Keyword match in message
        keywords = rules.get("keywords", [])
        if keywords:
            text_lower = message_text.lower()
            for keyword in keywords:
                if keyword.lower() in text_lower:
                    return True

        return False

    async def forward_to_agent(
        self, agent_name: str, payload: dict
    ) -> bool:
        """Forward an inbound message to an agent's callback URL.

        Args:
            agent_name: The agent to forward to
            payload: The message data to send

        Returns:
            True if forwarded successfully
        """
        result = await self._session.execute(
            select(AgentRegistration).where(
                AgentRegistration.name == agent_name,
                AgentRegistration.is_active == True,
            )
        )
        reg = result.scalar_one_or_none()
        if not reg:
            logger.warning(f"Agent '{agent_name}' not registered or inactive")
            return False

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(reg.callback_url, json=payload)
                if resp.status_code in (200, 201, 202):
                    return True
                logger.warning(
                    f"Agent '{agent_name}' callback returned {resp.status_code}"
                )
                return False
        except Exception as e:
            logger.error(f"Failed to forward to '{agent_name}': {e}")
            return False
