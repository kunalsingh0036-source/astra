"""
Cooldown model — prevents agents from bombarding the same contact.

Two levels:
1. Global cooldown: Any agent → same contact (default 4 hours)
2. Per-agent cooldown: Same agent → same contact (default 24 hours)

The gateway checks cooldowns before dispatching any outbound message.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from gateway.db.engine import Base


class Cooldown(Base):
    __tablename__ = "cooldowns"
    __table_args__ = (
        UniqueConstraint("contact_id", "agent_name", name="uq_cooldown_contact_agent"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id"), nullable=False, index=True,
    )
    agent_name: Mapped[str] = mapped_column(
        String(100), nullable=False, index=True,
    )
    last_sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    next_allowed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    contact: Mapped["Contact"] = relationship(
        "Contact", back_populates="cooldowns"
    )
