"""Payment schemas."""

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from finance.models.payment import PaymentMode, PaymentStatus


class PaymentCreate(BaseModel):
    business_id: uuid.UUID
    invoice_id: uuid.UUID | None = None
    bank_account_id: uuid.UUID | None = None
    amount: Decimal = Field(..., ge=0)
    payment_date: date
    payment_mode: PaymentMode = PaymentMode.UPI
    reference_number: str | None = Field(None, max_length=100)
    status: PaymentStatus = PaymentStatus.PENDING
    counterparty_name: str | None = Field(None, max_length=200)
    description: str | None = Field(None, max_length=500)
    extra_data: dict = Field(default_factory=dict)


class PaymentUpdate(BaseModel):
    status: PaymentStatus | None = None
    reference_number: str | None = None
    description: str | None = None
    extra_data: dict | None = None


class PaymentOut(BaseModel):
    id: uuid.UUID
    business_id: uuid.UUID
    invoice_id: uuid.UUID | None
    bank_account_id: uuid.UUID | None
    amount: Decimal
    payment_date: date
    payment_mode: PaymentMode
    reference_number: str | None
    status: PaymentStatus
    counterparty_name: str | None
    description: str | None
    extra_data: dict
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
