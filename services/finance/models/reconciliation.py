"""Reconciliation records — bank and GST."""

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, Enum, ForeignKey, Integer, Numeric, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from finance.db.engine import Base


class ReconciliationType(str, enum.Enum):
    BANK = "bank"
    GST_2B = "gst_2b"
    GST_3B = "gst_3b"


class ReconciliationStatus(str, enum.Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    HAS_DISCREPANCIES = "has_discrepancies"


class Reconciliation(Base):
    __tablename__ = "reconciliations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id"), nullable=False
    )
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    type: Mapped[ReconciliationType] = mapped_column(
        Enum(ReconciliationType), nullable=False
    )
    status: Mapped[ReconciliationStatus] = mapped_column(
        Enum(ReconciliationStatus), default=ReconciliationStatus.PENDING
    )
    total_matched: Mapped[int] = mapped_column(Integer, default=0)
    total_unmatched: Mapped[int] = mapped_column(Integer, default=0)
    discrepancy_amount: Mapped[Decimal] = mapped_column(
        Numeric(15, 2), default=Decimal("0.00")
    )
    report_data: Mapped[dict] = mapped_column(JSONB, default=dict)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Relationships
    business = relationship("Business")
