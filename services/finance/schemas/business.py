"""Business schemas."""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from finance.models.business import BusinessType


class BusinessCreate(BaseModel):
    name: str = Field(..., max_length=200)
    slug: str = Field(..., max_length=50)
    gstin: str | None = Field(None, max_length=20)
    pan: str | None = Field(None, max_length=15)
    business_type: BusinessType = BusinessType.PROPRIETORSHIP


class BusinessUpdate(BaseModel):
    name: str | None = Field(None, max_length=200)
    gstin: str | None = None
    pan: str | None = None
    business_type: BusinessType | None = None


class BusinessOut(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    gstin: str | None
    pan: str | None
    business_type: BusinessType
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
