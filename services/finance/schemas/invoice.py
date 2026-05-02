"""Invoice schemas."""

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from finance.models.invoice import InvoiceStatus, InvoiceType


class InvoiceCreate(BaseModel):
    business_id: uuid.UUID
    invoice_number: str = Field(..., max_length=50)
    type: InvoiceType
    counterparty_name: str = Field(..., max_length=200)
    counterparty_gstin: str | None = Field(None, max_length=20)
    amount: Decimal = Field(..., ge=0)
    tax_amount: Decimal = Field(default=Decimal("0.00"), ge=0)
    total_amount: Decimal = Field(..., ge=0)
    currency: str = Field(default="INR", max_length=5)
    issue_date: date
    due_date: date
    status: InvoiceStatus = InvoiceStatus.DRAFT
    description: str | None = None
    bookkeeper_ref_id: str | None = None
    extra_data: dict = Field(default_factory=dict)


class InvoiceUpdate(BaseModel):
    status: InvoiceStatus | None = None
    payment_received: Decimal | None = None
    counterparty_name: str | None = None
    counterparty_gstin: str | None = None
    due_date: date | None = None
    description: str | None = None
    extra_data: dict | None = None


class InvoiceOut(BaseModel):
    id: uuid.UUID
    business_id: uuid.UUID
    invoice_number: str
    type: InvoiceType
    counterparty_name: str
    counterparty_gstin: str | None
    amount: Decimal
    tax_amount: Decimal
    total_amount: Decimal
    currency: str
    issue_date: date
    due_date: date
    status: InvoiceStatus
    payment_received: Decimal
    balance_due: Decimal
    is_overdue: bool
    description: str | None
    bookkeeper_ref_id: str | None
    extra_data: dict
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class InvoiceSummary(BaseModel):
    total_receivable: Decimal
    total_payable: Decimal
    overdue_count: int
    overdue_amount: Decimal
