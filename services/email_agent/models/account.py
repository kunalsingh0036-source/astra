"""Email account — Gmail accounts connected to Astra."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from email_agent.db.engine import Base


class EmailAccount(Base):
    __tablename__ = "email_accounts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    email_address: Mapped[str] = mapped_column(
        String(255), nullable=False, unique=True, index=True
    )
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Gmail sync state
    gmail_history_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # OAuth tokens stored as encrypted text (in production, use a secrets manager)
    access_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_expiry: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    messages = relationship("EmailMessage", back_populates="account")
    threads = relationship("EmailThread", back_populates="account")

    def __repr__(self):
        return f"<EmailAccount({self.email_address})>"
