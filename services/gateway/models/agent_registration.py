"""
Agent registration — how agents plug into the gateway.

Each agent registers with a callback URL and claim rules.
When an inbound message needs routing, the gateway checks
registrations to find the right handler.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from gateway.db.engine import Base


class AgentRegistration(Base):
    __tablename__ = "agent_registrations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(
        String(100), unique=True, nullable=False, index=True,
        comment="Agent name (e.g., helmtech-outreach, apex-outreach)",
    )
    callback_url: Mapped[str] = mapped_column(
        String(500), nullable=False,
        comment="URL to POST inbound messages to (e.g., http://localhost:8003/webhooks/whatsapp)",
    )
    api_key: Mapped[str] = mapped_column(
        String(200), nullable=False,
        comment="Key the agent uses to authenticate with the gateway",
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int] = mapped_column(
        default=5,
        comment="1-10, higher = gets first pick when routing ambiguous messages",
    )
    claim_rules: Mapped[dict | None] = mapped_column(
        JSONB, default=dict,
        comment="Rules for claiming unassigned contacts: {phone_prefixes: [], keywords: []}",
    )
    daily_limit: Mapped[int] = mapped_column(
        default=100,
        comment="Max outbound messages this agent can send per day",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
