"""Label — mirrors Gmail labels for local querying."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from email_agent.db.engine import Base


class Label(Base):
    __tablename__ = "labels"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    gmail_label_id: Mapped[str] = mapped_column(
        String(100), nullable=False, unique=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    label_type: Mapped[str] = mapped_column(
        String(20), default="user"
    )  # system, user
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    unread_count: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
