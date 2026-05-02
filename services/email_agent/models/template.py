"""Email template — reusable email templates with variable substitution."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from email_agent.db.engine import Base


class EmailTemplate(Base):
    __tablename__ = "email_templates"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(String(300), nullable=True)
    subject_template: Mapped[str] = mapped_column(String(500), nullable=False)
    body_template: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str | None] = mapped_column(
        String(50), nullable=True
    )  # follow_up, introduction, proposal, reminder, etc.
    variables: Mapped[dict] = mapped_column(
        JSONB, default=dict
    )  # {"name": "Recipient name", "company": "Company name"}
    extra_data: Mapped[dict] = mapped_column(JSONB, default=dict)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
