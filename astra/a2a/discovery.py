"""
Agent discovery — resolving Agent Cards from URLs.

When Astra needs to talk to an agent, it first fetches the Agent Card
from the standard location: {base_url}/.well-known/agent.json

This module handles:
- Fetching and validating Agent Cards
- Caching discovered agents (avoid re-fetching on every task)
- Health checking agents (are they still alive?)
- Local registration (for agents running on the same machine)

Discovery flow:
1. Astra has a URL for an agent (configured or found via registry)
2. Fetch {url}/.well-known/agent.json
3. Validate the Agent Card
4. Cache it for future use
5. Now Astra knows what skills this agent has and can route tasks to it
"""

import asyncio
import logging
import time
from datetime import datetime, timezone

import httpx

from astra.a2a.exceptions import (
    AgentCardInvalidError,
    AgentNotFoundError,
)
from astra.a2a.models import AgentCard

logger = logging.getLogger(__name__)


class DiscoveredAgent:
    """An agent that has been discovered and validated.

    Wraps an AgentCard with runtime metadata: when it was discovered,
    when it was last checked, and whether it's currently healthy.
    """

    __slots__ = ("card", "discovered_at", "last_health_check", "healthy")

    def __init__(self, card: AgentCard):
        self.card = card
        self.discovered_at = datetime.now(timezone.utc)
        self.last_health_check: datetime | None = None
        self.healthy: bool = True

    def to_dict(self) -> dict:
        return {
            "name": self.card.name,
            "url": self.card.url,
            "description": self.card.description,
            "skills": [s.id for s in self.card.skills],
            "healthy": self.healthy,
            "discovered_at": self.discovered_at.isoformat(),
            "last_health_check": (
                self.last_health_check.isoformat()
                if self.last_health_check
                else None
            ),
        }


class AgentDiscovery:
    """Discovers, validates, and caches A2A agent connections.

    This is Astra's "address book" for agents. It maintains a cache of
    known agents and their capabilities.

    Usage:
        discovery = AgentDiscovery()
        agent = await discovery.discover("http://localhost:8100")
        # agent.card.skills  → list of what it can do

        # Or register a local agent directly (no HTTP fetch needed)
        discovery.register_local(agent_card)
    """

    def __init__(self, cache_ttl_seconds: int = 300):
        # name → DiscoveredAgent
        self._cache: dict[str, DiscoveredAgent] = {}
        self._cache_ttl = cache_ttl_seconds
        self._http_client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Lazy-init the HTTP client."""
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(timeout=10.0)
        return self._http_client

    async def close(self) -> None:
        """Clean up the HTTP client."""
        if self._http_client and not self._http_client.is_closed:
            await self._http_client.aclose()

    async def discover(self, base_url: str) -> DiscoveredAgent:
        """Fetch and validate an Agent Card from a URL.

        Checks the cache first. If cached and not expired, returns
        the cached version. Otherwise fetches fresh.

        Args:
            base_url: The agent's base URL (e.g., http://localhost:8100)

        Returns:
            DiscoveredAgent with validated AgentCard

        Raises:
            AgentNotFoundError: Agent URL unreachable or no Agent Card
            AgentCardInvalidError: Agent Card failed validation
        """
        # Normalize URL
        base_url = base_url.rstrip("/")

        # Check cache
        for agent in self._cache.values():
            if agent.card.url == base_url:
                age = (
                    datetime.now(timezone.utc) - agent.discovered_at
                ).total_seconds()
                if age < self._cache_ttl:
                    logger.debug(
                        f"Cache hit for {agent.card.name} at {base_url}"
                    )
                    return agent

        # Fetch Agent Card
        card_url = f"{base_url}/.well-known/agent.json"
        client = await self._get_client()

        try:
            response = await client.get(card_url)
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise AgentNotFoundError(base_url) from e
        except httpx.ConnectError as e:
            raise AgentNotFoundError(base_url) from e
        except httpx.TimeoutException as e:
            raise AgentNotFoundError(base_url) from e

        # Parse and validate
        try:
            data = response.json()
            card = AgentCard(**data)
        except Exception as e:
            raise AgentCardInvalidError(base_url, str(e)) from e

        # Ensure the URL matches what we fetched from
        if not card.url:
            card.url = base_url

        # Cache it
        discovered = DiscoveredAgent(card)
        self._cache[card.name] = discovered
        logger.info(
            f"Discovered agent '{card.name}' at {base_url} "
            f"with {len(card.skills)} skills"
        )

        return discovered

    def register_local(self, card: AgentCard) -> DiscoveredAgent:
        """Register an agent directly without HTTP discovery.

        Use this for agents running in the same process or on the same
        machine where you already have the AgentCard object.

        Args:
            card: The agent's AgentCard

        Returns:
            DiscoveredAgent wrapping the card
        """
        discovered = DiscoveredAgent(card)
        self._cache[card.name] = discovered
        logger.info(f"Registered local agent '{card.name}'")
        return discovered

    def get(self, name: str) -> DiscoveredAgent | None:
        """Get a discovered agent by name."""
        return self._cache.get(name)

    def list_all(self) -> list[DiscoveredAgent]:
        """List all discovered agents."""
        return list(self._cache.values())

    def list_healthy(self) -> list[DiscoveredAgent]:
        """List only healthy agents."""
        return [a for a in self._cache.values() if a.healthy]

    async def health_check(self, name: str) -> bool:
        """Check if an agent is still alive.

        Sends a GET to {url}/a2a/health and expects a 200 response.

        Args:
            name: Agent name

        Returns:
            True if healthy, False otherwise
        """
        agent = self._cache.get(name)
        if not agent:
            return False

        client = await self._get_client()
        try:
            response = await client.get(
                f"{agent.card.url}/a2a/health",
                timeout=5.0,
            )
            agent.healthy = response.status_code == 200
        except Exception:
            agent.healthy = False

        agent.last_health_check = datetime.now(timezone.utc)
        return agent.healthy

    async def health_check_all(self) -> dict[str, bool]:
        """Health check all discovered agents concurrently.

        Returns:
            Dict of agent_name → is_healthy
        """
        results = {}
        tasks = []
        for name in self._cache:
            tasks.append(self.health_check(name))

        if tasks:
            checks = await asyncio.gather(*tasks, return_exceptions=True)
            for name, result in zip(self._cache.keys(), checks):
                if isinstance(result, Exception):
                    results[name] = False
                else:
                    results[name] = result

        return results

    def remove(self, name: str) -> bool:
        """Remove an agent from the discovery cache.

        Returns True if found and removed, False otherwise.
        """
        if name in self._cache:
            del self._cache[name]
            logger.info(f"Removed agent '{name}' from discovery cache")
            return True
        return False


# Global singleton — shared across the application
agent_discovery = AgentDiscovery()
