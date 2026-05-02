"""
Message model — every WhatsApp message in or out.

Tracks the full lifecycle: queued → sent → delivered → read → (replied).
Supports text, template, image, document, video, and button types.
"""

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Enum, Float, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from gateway.db.engine import Base


class MessageDirection(str, enum.Enum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class MessageType(str, enum.Enum):
    TEXT = "text"
    TEMPLATE = "template"
    IMAGE = "image"
    DOCUMENT = "document"
    VIDEO = "video"
    BUTTON = "button"
    INTERACTIVE = "interactive"


class MessageStatus(str, enum.Enum):
    QUEUED = "queued"           # In queue, not yet sent
    SENT = "sent"               # Sent to Meta API
    DELIVERED = "delivered"     # Delivered to device
    READ = "read"               # Read by recipient
    FAILED = "failed"           # Send failed
    REJECTED = "rejected"       # Rejected by cooldown/session/validation


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id"), nullable=False, index=True,
    )
    direction: Mapped[MessageDirection] = mapped_column(
        Enum(MessageDirection), nullable=False,
    )
    message_type: Mapped[MessageType] = mapped_column(
        Enum(MessageType), default=MessageType.TEXT,
    )
    content: Mapped[str | None] = mapped_column(
        Text, comment="Message body text",
    )
    template_name: Mapped[str | None] = mapped_column(
        String(200), comment="Meta template name (for template messages)",
    )
    status: Mapped[MessageStatus] = mapped_column(
        Enum(MessageStatus), default=MessageStatus.QUEUED,
    )
    external_id: Mapped[str | None] = mapped_column(
        String(500), unique=True,
        comment="Meta's wamid (WhatsApp Message ID) for deduplication",
    )
    agent_name: Mapped[str] = mapped_column(
        String(100), nullable=False, index=True,
        comment="Which agent sent (outbound) or should receive (inbound) this message",
    )

    # AI classification (for inbound messages)
    classification: Mapped[str | None] = mapped_column(
        String(50),
        comment="AI classification: interested, not_interested, question, complaint, opt_out, etc.",
    )
    classification_confidence: Mapped[float | None] = mapped_column(Float)

    # Flexible metadata
    extra_data: Mapped[dict | None] = mapped_column(
        JSONB, default=dict,
        comment="Template components, media URLs, error details, etc.",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    conversation: Mapped["Conversation"] = relationship(
        "Conversation", back_populates="messages"
    )
