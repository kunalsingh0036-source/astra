"""Reconciliation schemas."""

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel

from finance.models.reconciliation import ReconciliationStatus, ReconciliationType


class ReconciliationCreate(BaseModel):
    business_id: uuid.UUID
    period_start: date
    period_end: date
    type: ReconciliationType


class ReconciliationOut(BaseModel):
    id: uuid.UUID
    business_id: uuid.UUID
    period_start: date
    period_end: date
    type: ReconciliationType
    status: ReconciliationStatus
    total_matched: int
    total_unmatched: int
    discrepancy_amount: Decimal
    report_data: dict
    created_at: datetime
    completed_at: datetime | None

    model_config = {"from_attributes": True}
