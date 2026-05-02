"""Expense tracking with AI categorization."""

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean, Date, DateTime, Float, ForeignKey, Numeric, String, func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from finance.db.engine import Base


class Expense(Base):
    __tablename__ = "expenses"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id"), nullable=False
    )
    category: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    subcategory: Mapped[str | None] = mapped_column(String(50), nullable=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    tax_amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=Decimal("0.00"))
    vendor_name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    expense_date: Mapped[date] = mapped_column(Date, nullable=False)
    payment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payments.id"), nullable=True
    )
    is_recurring: Mapped[bool] = mapped_column(Boolean, default=False)
    recurrence_frequency: Mapped[str | None] = mapped_column(
        String(20), nullable=True
    )  # monthly, quarterly, annually
    ai_categorized: Mapped[bool] = mapped_column(Boolean, default=False)
    ai_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    extra_data: Mapped[dict] = mapped_column(JSONB, default=dict)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    business = relationship("Business", back_populates="expenses")
    payment = relationship("Payment")
