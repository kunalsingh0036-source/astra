"""
External agent registration.

Registers all external A2A agents in both:
1. Astra's fleet registry (for fleet management tools)
2. Astra's A2A discovery cache (for A2A protocol tools)

Called on Astra startup to populate the fleet with known agents.
"""

import logging

from astra.a2a.discovery import agent_discovery
from astra.agents.registry import AgentDefinitionRecord, AgentStatus, agent_registry

from astra.agents.external.bookkeeper import CARD as BOOKKEEPER_CARD
from astra.agents.external.linkedin import CARD as LINKEDIN_CARD
from astra.agents.external.helmtech import CARD as HELMTECH_CARD
from astra.agents.external.apex import CARD as APEX_CARD
from astra.agents.external.whatsapp import CARD as WHATSAPP_CARD
from astra.agents.external.finance import CARD as FINANCE_CARD
from astra.agents.external.email_agent import CARD as EMAIL_CARD

logger = logging.getLogger(__name__)

# All external agent cards
EXTERNAL_AGENTS = [
    BOOKKEEPER_CARD,
    LINKEDIN_CARD,
    HELMTECH_CARD,
    APEX_CARD,
    WHATSAPP_CARD,
    FINANCE_CARD,
    EMAIL_CARD,
]


def register_all_external_agents() -> int:
    """Register all external agents in both the fleet and A2A discovery.

    Returns:
        Number of agents registered.
    """
    count = 0

    for card in EXTERNAL_AGENTS:
        # Register in fleet registry (for fleet management tools)
        agent_registry.register(
            AgentDefinitionRecord(
                name=card.name,
                description=card.description,
                capabilities=[s.description for s in card.skills],
                status=AgentStatus.ACTIVE,
                tools=[s.id for s in card.skills],
                model_tier=card.model_tier,
                build_complexity="external",
            )
        )

        # Register in A2A discovery (for A2A protocol tools)
        agent_discovery.register_local(card)

        logger.info(
            f"Registered external agent '{card.name}' "
            f"({len(card.skills)} skills, tier={card.model_tier})"
        )
        count += 1

    logger.info(f"Registered {count} external agents")
    return count


def get_all_cards():
    """Get all external agent cards (for documentation/introspection)."""
    return EXTERNAL_AGENTS
