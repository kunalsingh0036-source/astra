"""Daily cash flow snapshots with AI forecasting."""

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Index, Numeric, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from finance.db.engine import Base


class CashFlowSnapshot(Base):
    __tablename__ = "cash_flow_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id"), nullable=False
    )
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    inflow: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=Decimal("0.00"))
    outflow: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=Decimal("0.00"))
    net_flow: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=Decimal("0.00"))
    running_balance: Mapped[Decimal] = mapped_column(
        Numeric(15, 2), default=Decimal("0.00")
    )

    # AI-predicted forecasts
    forecast_30d: Mapped[Decimal | None] = mapped_column(Numeric(15, 2), nullable=True)
    forecast_60d: Mapped[Decimal | None] = mapped_column(Numeric(15, 2), nullable=True)
    forecast_90d: Mapped[Decimal | None] = mapped_column(Numeric(15, 2), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    business = relationship("Business")

    __table_args__ = (
        Index("ix_cash_flow_business_date", "business_id", "snapshot_date", unique=True),
    )
