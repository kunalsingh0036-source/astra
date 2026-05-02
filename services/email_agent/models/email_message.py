"""Email message — cached copy of Gmail messages."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean, DateTime, Enum, ForeignKey, Index, String, Text, func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from email_agent.db.engine import Base


class EmailDirection(str, enum.Enum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class EmailMessage(Base):
    __tablename__ = "email_messages"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("email_accounts.id"), nullable=False
    )
    thread_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("email_threads.id"), nullable=True
    )

    # Gmail identifiers
    gmail_message_id: Mapped[str] = mapped_column(
        String(100), nullable=False, unique=True, index=True
    )
    gmail_thread_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)

    # Headers
    direction: Mapped[EmailDirection] = mapped_column(
        Enum(EmailDirection), nullable=False
    )
    from_address: Mapped[str] = mapped_column(String(255), nullable=False)
    to_addresses: Mapped[list[str]] = mapped_column(ARRAY(String(255)), default=list)
    cc_addresses: Mapped[list[str]] = mapped_column(ARRAY(String(255)), default=list)
    bcc_addresses: Mapped[list[str]] = mapped_column(ARRAY(String(255)), default=list)
    subject: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    in_reply_to: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Body
    body_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    snippet: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Metadata
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False)
    is_starred: Mapped[bool] = mapped_column(Boolean, default=False)
    is_draft: Mapped[bool] = mapped_column(Boolean, default=False)
    has_attachments: Mapped[bool] = mapped_column(Boolean, default=False)
    gmail_labels: Mapped[list[str]] = mapped_column(ARRAY(String(100)), default=list)

    # AI classification
    ai_category: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    ai_priority: Mapped[str | None] = mapped_column(String(20), nullable=True)
    ai_summary: Mapped[str | None] = mapped_column(String(500), nullable=True)
    ai_action_needed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    extra_data: Mapped[dict] = mapped_column(JSONB, default=dict)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    account = relationship("EmailAccount", back_populates="messages")
    thread = relationship("EmailThread", back_populates="messages")

    __table_args__ = (
        Index("ix_email_messages_account_sent", "account_id", "sent_at"),
        Index("ix_email_messages_direction", "direction"),
    )
