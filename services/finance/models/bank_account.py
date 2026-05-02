"""Bank account tracking across businesses."""

import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, Enum, ForeignKey, Numeric, String, Boolean, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from finance.db.engine import Base


class AccountType(str, enum.Enum):
    CURRENT = "current"
    SAVINGS = "savings"
    CREDIT_CARD = "credit_card"
    OVERDRAFT = "overdraft"


class BankAccount(Base):
    __tablename__ = "bank_accounts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id"), nullable=False
    )
    bank_name: Mapped[str] = mapped_column(String(100), nullable=False)
    account_number: Mapped[str] = mapped_column(String(50), nullable=False)
    ifsc_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    account_type: Mapped[AccountType] = mapped_column(
        Enum(AccountType), default=AccountType.CURRENT
    )
    current_balance: Mapped[Decimal] = mapped_column(
        Numeric(15, 2), default=Decimal("0.00")
    )
    balance_as_of: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    business = relationship("Business", back_populates="bank_accounts")
    payments = relationship("Payment", back_populates="bank_account")
