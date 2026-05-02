"""Bank account schemas."""

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from finance.models.bank_account import AccountType


class BankAccountCreate(BaseModel):
    business_id: uuid.UUID
    bank_name: str = Field(..., max_length=100)
    account_number: str = Field(..., max_length=30)
    ifsc_code: str = Field(..., max_length=15)
    account_type: AccountType = AccountType.CURRENT
    current_balance: Decimal = Field(default=Decimal("0.00"))
    is_primary: bool = False


class BankAccountUpdate(BaseModel):
    current_balance: Decimal | None = None
    is_primary: bool | None = None
    bank_name: str | None = None


class BankAccountOut(BaseModel):
    id: uuid.UUID
    business_id: uuid.UUID
    bank_name: str
    account_number: str
    ifsc_code: str
    account_type: AccountType
    current_balance: Decimal
    is_primary: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
