"""Invoice tracking — receivables and payables across businesses."""

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Date, DateTime, Enum, ForeignKey, Index, Numeric, String, Text, func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from finance.db.engine import Base


class InvoiceType(str, enum.Enum):
    RECEIVABLE = "receivable"
    PAYABLE = "payable"


class InvoiceStatus(str, enum.Enum):
    DRAFT = "draft"
    SENT = "sent"
    PARTIALLY_PAID = "partially_paid"
    PAID = "paid"
    OVERDUE = "overdue"
    CANCELLED = "cancelled"


class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id"), nullable=False
    )
    invoice_number: Mapped[str] = mapped_column(String(50), nullable=False)
    type: Mapped[InvoiceType] = mapped_column(Enum(InvoiceType), nullable=False)
    counterparty_name: Mapped[str] = mapped_column(String(200), nullable=False)
    counterparty_gstin: Mapped[str | None] = mapped_column(String(20), nullable=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    tax_amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=Decimal("0.00"))
    total_amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(5), default="INR")
    issue_date: Mapped[date] = mapped_column(Date, nullable=False)
    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[InvoiceStatus] = mapped_column(
        Enum(InvoiceStatus), default=InvoiceStatus.DRAFT, index=True
    )
    payment_received: Mapped[Decimal] = mapped_column(
        Numeric(15, 2), default=Decimal("0.00")
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    bookkeeper_ref_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    extra_data: Mapped[dict] = mapped_column(JSONB, default=dict)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    business = relationship("Business", back_populates="invoices")
    payments = relationship("Payment", back_populates="invoice")

    __table_args__ = (
        Index("ix_invoices_due_date_status", "due_date", "status"),
        Index("ix_invoices_business_status", "business_id", "status"),
    )

    @property
    def balance_due(self) -> Decimal:
        return self.total_amount - self.payment_received

    @property
    def is_overdue(self) -> bool:
        return (
            self.status not in (InvoiceStatus.PAID, InvoiceStatus.CANCELLED)
            and self.due_date < date.today()
        )
