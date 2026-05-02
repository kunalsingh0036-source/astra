"""Cash flow schemas."""

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel


class CashFlowSnapshotOut(BaseModel):
    id: uuid.UUID
    business_id: uuid.UUID
    snapshot_date: date
    inflow: Decimal
    outflow: Decimal
    net_flow: Decimal
    running_balance: Decimal
    forecast_30d: Decimal | None
    forecast_60d: Decimal | None
    forecast_90d: Decimal | None
    created_at: datetime

    model_config = {"from_attributes": True}


class CashFlowSummary(BaseModel):
    current_balance: Decimal
    inflow_30d: Decimal
    outflow_30d: Decimal
    net_flow_30d: Decimal
    forecast_30d: Decimal | None
    forecast_60d: Decimal | None
    forecast_90d: Decimal | None
