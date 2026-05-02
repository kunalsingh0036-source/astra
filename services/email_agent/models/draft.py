"""Draft — AI-generated email drafts awaiting review/send."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from email_agent.db.engine import Base


class DraftStatus(str, enum.Enum):
    GENERATING = "generating"
    READY = "ready"
    APPROVED = "approved"
    SENT = "sent"
    DISCARDED = "discarded"


class Draft(Base):
    __tablename__ = "drafts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("email_accounts.id"), nullable=False
    )
    reply_to_message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("email_messages.id"), nullable=True
    )

    to_addresses: Mapped[list[str]] = mapped_column(ARRAY(String(255)), default=list)
    cc_addresses: Mapped[list[str]] = mapped_column(ARRAY(String(255)), default=list)
    subject: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    body_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    body_html: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[DraftStatus] = mapped_column(
        Enum(DraftStatus), default=DraftStatus.GENERATING
    )

    # AI generation context
    prompt_used: Mapped[str | None] = mapped_column(Text, nullable=True)
    tone: Mapped[str | None] = mapped_column(String(50), nullable=True)  # formal, casual, friendly, firm
    extra_data: Mapped[dict] = mapped_column(JSONB, default=dict)

    # Gmail draft ID (once saved to Gmail)
    gmail_draft_id: Mapped[str | None] = mapped_column(String(100), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    account = relationship("EmailAccount")
    reply_to = relationship("EmailMessage")
