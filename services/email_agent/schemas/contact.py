"""Contact schemas."""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class ContactCreate(BaseModel):
    email_address: str = Field(..., max_length=255)
    display_name: str | None = Field(None, max_length=200)
    company: str | None = Field(None, max_length=200)
    role: str | None = Field(None, max_length=100)
    category: str | None = Field(None, max_length=50)


class ContactUpdate(BaseModel):
    display_name: str | None = None
    company: str | None = None
    role: str | None = None
    category: str | None = None


class ContactOut(BaseModel):
    id: uuid.UUID
    email_address: str
    display_name: str | None
    company: str | None
    role: str | None
    category: str | None
    emails_received: int
    emails_sent: int
    last_contacted_at: datetime | None
    avg_response_time_hours: float | None
    created_at: datetime

    model_config = {"from_attributes": True}
