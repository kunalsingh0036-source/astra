"""Financial alerts — AI-triggered warnings and notifications."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from finance.db.engine import Base


class AlertType(str, enum.Enum):
    CASH_LOW = "cash_low"
    INVOICE_OVERDUE = "invoice_overdue"
    PAYMENT_FAILED = "payment_failed"
    RECONCILIATION_MISMATCH = "reconciliation_mismatch"
    UNUSUAL_EXPENSE = "unusual_expense"
    GST_DEADLINE = "gst_deadline"
    RECURRING_PAYMENT_DUE = "recurring_payment_due"
    FORECAST_WARNING = "forecast_warning"


class AlertSeverity(str, enum.Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id"), nullable=False
    )
    type: Mapped[AlertType] = mapped_column(Enum(AlertType), nullable=False, index=True)
    severity: Mapped[AlertSeverity] = mapped_column(
        Enum(AlertSeverity), default=AlertSeverity.WARNING
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False)
    is_resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    related_entity_type: Mapped[str | None] = mapped_column(
        String(50), nullable=True
    )  # "invoice", "payment", "expense", etc.
    related_entity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    extra_data: Mapped[dict] = mapped_column(JSONB, default=dict)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Relationships
    business = relationship("Business", back_populates="alerts")
