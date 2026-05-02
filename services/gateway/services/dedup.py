"""
Cross-agent deduplication and cooldown enforcement.

Prevents multiple agents from bombarding the same contact.
Two cooldown levels:
1. Global: Any agent → same contact (default 4 hours)
2. Per-agent: Same agent → same contact (default 24 hours)
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.config import settings
from gateway.models.cooldown import Cooldown


class CooldownService:
    """Manages cross-agent message cooldowns."""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def can_send(
        self, contact_id, agent_name: str
    ) -> tuple[bool, str]:
        """Check if this agent can send to this contact right now.

        Returns:
            (allowed: bool, reason: str)
        """
        now = datetime.now(timezone.utc)

        # Check per-agent cooldown (same agent → same contact)
        agent_cd = await self._session.execute(
            select(Cooldown).where(
                and_(
                    Cooldown.contact_id == contact_id,
                    Cooldown.agent_name == agent_name,
                    Cooldown.next_allowed_at > now,
                )
            )
        )
        agent_cooldown = agent_cd.scalar_one_or_none()
        if agent_cooldown:
            remaining = (agent_cooldown.next_allowed_at - now).total_seconds() / 3600
            return False, f"Agent cooldown: {remaining:.1f}h remaining"

        # Check global cooldown (any agent → same contact)
        global_cd = await self._session.execute(
            select(Cooldown).where(
                and_(
                    Cooldown.contact_id == contact_id,
                    Cooldown.next_allowed_at > now,
                )
            )
        )
        global_cooldown = global_cd.scalar_one_or_none()
        if global_cooldown:
            remaining = (global_cooldown.next_allowed_at - now).total_seconds() / 3600
            return False, (
                f"Global cooldown: another agent sent {remaining:.1f}h ago. "
                f"Agent '{global_cooldown.agent_name}' owns the window."
            )

        return True, "No active cooldowns"

    async def record_send(
        self,
        contact_id,
        agent_name: str,
        cooldown_hours: int | None = None,
    ) -> None:
        """Record that a message was sent, creating/updating cooldown.

        Args:
            contact_id: Contact UUID
            agent_name: Which agent sent
            cooldown_hours: Override default cooldown (uses agent_cooldown_hours)
        """
        now = datetime.now(timezone.utc)
        hours = cooldown_hours or settings.agent_cooldown_hours
        next_allowed = now + timedelta(hours=hours)

        # Upsert: update if exists, insert if not
        existing = await self._session.execute(
            select(Cooldown).where(
                and_(
                    Cooldown.contact_id == contact_id,
                    Cooldown.agent_name == agent_name,
                )
            )
        )
        cooldown = existing.scalar_one_or_none()

        if cooldown:
            cooldown.last_sent_at = now
            cooldown.next_allowed_at = next_allowed
        else:
            cooldown = Cooldown(
                contact_id=contact_id,
                agent_name=agent_name,
                last_sent_at=now,
                next_allowed_at=next_allowed,
            )
            self._session.add(cooldown)

        await self._session.flush()

    async def get_active_cooldowns(self, contact_id) -> list[dict]:
        """Get all active cooldowns for a contact."""
        now = datetime.now(timezone.utc)
        result = await self._session.execute(
            select(Cooldown).where(
                and_(
                    Cooldown.contact_id == contact_id,
                    Cooldown.next_allowed_at > now,
                )
            )
        )
        cooldowns = result.scalars().all()
        return [
            {
                "agent": cd.agent_name,
                "last_sent": cd.last_sent_at.isoformat(),
                "allowed_at": cd.next_allowed_at.isoformat(),
                "remaining_hours": (cd.next_allowed_at - now).total_seconds() / 3600,
            }
            for cd in cooldowns
        ]
