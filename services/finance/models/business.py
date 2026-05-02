"""Business entity — each of Kunal's 3 businesses."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from finance.db.engine import Base


class BusinessType(str, enum.Enum):
    PROPRIETORSHIP = "proprietorship"
    PVT_LTD = "pvt_ltd"
    LLP = "llp"
    PARTNERSHIP = "partnership"


class Business(Base):
    __tablename__ = "businesses"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    slug: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    gstin: Mapped[str | None] = mapped_column(String(20), nullable=True)
    pan: Mapped[str | None] = mapped_column(String(15), nullable=True)
    business_type: Mapped[BusinessType] = mapped_column(
        Enum(BusinessType), default=BusinessType.PROPRIETORSHIP
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    bank_accounts = relationship("BankAccount", back_populates="business")
    invoices = relationship("Invoice", back_populates="business")
    payments = relationship("Payment", back_populates="business")
    expenses = relationship("Expense", back_populates="business")
    alerts = relationship("Alert", back_populates="business")

    def __repr__(self):
        return f"<Business(name='{self.name}')>"
