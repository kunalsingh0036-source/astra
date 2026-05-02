"""Email thread — groups related messages (mirrors Gmail threads)."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from email_agent.db.engine import Base


class ThreadPriority(str, enum.Enum):
    URGENT = "urgent"
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"


class EmailThread(Base):
    __tablename__ = "email_threads"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("email_accounts.id"), nullable=False
    )
    gmail_thread_id: Mapped[str] = mapped_column(
        String(100), nullable=False, unique=True, index=True
    )
    subject: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    participants: Mapped[list[str]] = mapped_column(ARRAY(String(255)), default=list)
    message_count: Mapped[int] = mapped_column(Integer, default=0)

    # Timestamps
    first_message_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_message_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    # AI analysis
    ai_priority: Mapped[ThreadPriority] = mapped_column(
        Enum(ThreadPriority), default=ThreadPriority.NORMAL
    )
    ai_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_category: Mapped[str | None] = mapped_column(String(50), nullable=True)
    needs_response: Mapped[bool | None] = mapped_column(default=None)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    account = relationship("EmailAccount", back_populates="threads")
    messages = relationship("EmailMessage", back_populates="thread", order_by="EmailMessage.sent_at")
