"""
Contact model — the unified address book.

Every phone number that interacts with the gateway gets a Contact record.
Multiple agents may reference the same contact. The source_agent tracks
who created it first.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from gateway.db.engine import Base


class Contact(Base):
    __tablename__ = "contacts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    phone: Mapped[str] = mapped_column(
        String(20), unique=True, index=True, nullable=False,
        comment="E.164 format (e.g., +919876543210)",
    )
    name: Mapped[str | None] = mapped_column(String(200))
    country_code: Mapped[str | None] = mapped_column(
        String(5), comment="ISO country code (e.g., IN, US)"
    )
    source_agent: Mapped[str] = mapped_column(
        String(100), nullable=False,
        comment="Agent that created this contact first",
    )
    extra_data: Mapped[dict | None] = mapped_column(JSONB, default=dict)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    conversations: Mapped[list["Conversation"]] = relationship(
        "Conversation", back_populates="contact", lazy="selectin"
    )
    cooldowns: Mapped[list["Cooldown"]] = relationship(
        "Cooldown", back_populates="contact", lazy="selectin"
    )
