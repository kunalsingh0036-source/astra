"""Contact — known email contacts with interaction stats."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from email_agent.db.engine import Base


class Contact(Base):
    __tablename__ = "contacts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    email_address: Mapped[str] = mapped_column(
        String(255), nullable=False, unique=True, index=True
    )
    display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    company: Mapped[str | None] = mapped_column(String(200), nullable=True)
    role: Mapped[str | None] = mapped_column(String(100), nullable=True)
    category: Mapped[str | None] = mapped_column(
        String(50), nullable=True, index=True
    )  # client, vendor, team, personal, newsletter, etc.

    # Interaction stats
    emails_received: Mapped[int] = mapped_column(Integer, default=0)
    emails_sent: Mapped[int] = mapped_column(Integer, default=0)
    last_contacted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    avg_response_time_hours: Mapped[float | None] = mapped_column(nullable=True)

    extra_data: Mapped[dict] = mapped_column(JSONB, default=dict)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
