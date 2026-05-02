"""
Template model — Meta-approved WhatsApp message templates.

Templates are required for outbound messages outside the 24hr session window.
They must be pre-approved by Meta. This table is synced periodically from
Meta's template API.
"""

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Enum, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from gateway.db.engine import Base


class TemplateStatus(str, enum.Enum):
    APPROVED = "approved"
    PENDING = "pending"
    REJECTED = "rejected"


class Template(Base):
    __tablename__ = "templates"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(
        String(200), unique=True, nullable=False, index=True,
    )
    language: Mapped[str] = mapped_column(
        String(10), default="en", comment="e.g., en, hi, en_US"
    )
    category: Mapped[str] = mapped_column(
        String(50), default="marketing",
        comment="marketing, utility, authentication",
    )
    components: Mapped[dict] = mapped_column(
        JSONB, default=list,
        comment="Template components as returned by Meta API",
    )
    meta_status: Mapped[TemplateStatus] = mapped_column(
        Enum(TemplateStatus), default=TemplateStatus.PENDING,
    )
    agent_tags: Mapped[list | None] = mapped_column(
        JSONB, default=list,
        comment="Which agents can use this template (empty = all)",
    )
    last_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
