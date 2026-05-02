"""
Conversation model — threaded message groups per contact.

A conversation groups all messages between the gateway and a single contact.
The owning_agent tracks which agent "owns" this conversation (handles replies).
session_expires_at is the heart of Meta's 24-hour window rule.
"""

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from gateway.db.engine import Base


class ConversationStatus(str, enum.Enum):
    ACTIVE = "active"       # Ongoing conversation
    PENDING = "pending"     # New inbound, no agent assigned yet
    CLOSED = "closed"       # Manually or auto-closed


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id"), nullable=False, index=True,
    )
    owning_agent: Mapped[str | None] = mapped_column(
        String(100), index=True,
        comment="Agent that owns this conversation (handles replies). Null = unassigned.",
    )
    status: Mapped[ConversationStatus] = mapped_column(
        Enum(ConversationStatus), default=ConversationStatus.ACTIVE,
    )

    # 24-hour session window — the key business rule
    session_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        comment="When the 24hr session window expires. Set on every inbound message.",
    )
    last_customer_message_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        comment="When the customer last sent a message.",
    )
    last_message_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        comment="When any message (in or out) was last exchanged.",
    )
    message_count: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    contact: Mapped["Contact"] = relationship(
        "Contact", back_populates="conversations"
    )
    messages: Mapped[list["Message"]] = relationship(
        "Message", back_populates="conversation", lazy="selectin",
        order_by="Message.created_at",
    )
