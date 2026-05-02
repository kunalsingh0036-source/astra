"""
Session window manager — enforces Meta's 24-hour rule.

Meta's rule: You can send free-form text messages ONLY within 24 hours
of the customer's last inbound message. Outside that window, you MUST
use pre-approved template messages.

This service tracks the window per conversation and validates outbound
messages before they're sent.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.config import settings
from gateway.models.conversation import Conversation
from gateway.models.message import MessageType


class SessionManager:
    """Manages 24-hour session windows per conversation."""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def is_session_open(self, conversation_id) -> bool:
        """Check if the 24hr session window is currently open."""
        conv = await self._session.get(Conversation, conversation_id)
        if not conv or not conv.session_expires_at:
            return False
        return conv.session_expires_at > datetime.now(timezone.utc)

    async def open_session(self, conversation_id) -> datetime:
        """Open/extend the session window. Called when a customer message arrives.

        Returns the new expiry time.
        """
        conv = await self._session.get(Conversation, conversation_id)
        if not conv:
            raise ValueError(f"Conversation {conversation_id} not found")

        now = datetime.now(timezone.utc)
        expires = now + timedelta(hours=settings.session_window_hours)

        conv.session_expires_at = expires
        conv.last_customer_message_at = now
        conv.last_message_at = now

        await self._session.flush()
        return expires

    async def validate_outbound(
        self, conversation_id, message_type: MessageType
    ) -> tuple[bool, str]:
        """Check if an outbound message type is allowed right now.

        Returns:
            (allowed: bool, reason: str)
        """
        # Templates always allowed
        if message_type == MessageType.TEMPLATE:
            return True, "Template messages are always allowed"

        # For non-template messages, check session window
        is_open = await self.is_session_open(conversation_id)
        if is_open:
            return True, "Session window is open"

        return False, (
            f"24hr session window expired. "
            f"Use a template message or wait for customer reply."
        )
