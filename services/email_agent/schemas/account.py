"""Email account schemas."""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class AccountCreate(BaseModel):
    email_address: str = Field(..., max_length=255)
    display_name: str = Field(..., max_length=200)
    is_primary: bool = False


class AccountOut(BaseModel):
    id: uuid.UUID
    email_address: str
    display_name: str
    is_primary: bool
    is_active: bool
    last_sync_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}
