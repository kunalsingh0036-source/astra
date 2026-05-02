"""Expense schemas."""

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field


class ExpenseCreate(BaseModel):
    business_id: uuid.UUID
    category: str = Field(..., max_length=50)
    subcategory: str | None = Field(None, max_length=50)
    amount: Decimal = Field(..., ge=0)
    tax_amount: Decimal = Field(default=Decimal("0.00"), ge=0)
    vendor_name: str = Field(..., max_length=200)
    description: str | None = Field(None, max_length=500)
    expense_date: date
    payment_id: uuid.UUID | None = None
    is_recurring: bool = False
    recurrence_frequency: str | None = Field(None, max_length=20)
    extra_data: dict = Field(default_factory=dict)


class ExpenseUpdate(BaseModel):
    category: str | None = None
    subcategory: str | None = None
    amount: Decimal | None = None
    tax_amount: Decimal | None = None
    vendor_name: str | None = None
    description: str | None = None
    is_recurring: bool | None = None
    recurrence_frequency: str | None = None
    extra_data: dict | None = None


class ExpenseOut(BaseModel):
    id: uuid.UUID
    business_id: uuid.UUID
    category: str
    subcategory: str | None
    amount: Decimal
    tax_amount: Decimal
    vendor_name: str
    description: str | None
    expense_date: date
    payment_id: uuid.UUID | None
    is_recurring: bool
    recurrence_frequency: str | None
    ai_categorized: bool
    ai_confidence: float | None
    extra_data: dict
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ExpenseSummary(BaseModel):
    total_amount: Decimal
    by_category: dict[str, Decimal]
    recurring_monthly: Decimal
