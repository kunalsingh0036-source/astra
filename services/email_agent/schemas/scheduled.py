"""Scheduled email schemas."""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from email_agent.models.scheduled import ScheduleStatus


class ScheduleCreate(BaseModel):
    account_id: uuid.UUID
    to_addresses: list[str]
    cc_addresses: list[str] = Field(default_factory=list)
    subject: str
    body_text: str
    body_html: str | None = None
    scheduled_for: datetime
    draft_id: uuid.UUID | None = None


class ScheduledOut(BaseModel):
    id: uuid.UUID
    account_id: uuid.UUID
    draft_id: uuid.UUID | None
    to_addresses: list[str]
    cc_addresses: list[str]
    subject: str
    body_text: str
    scheduled_for: datetime
    status: ScheduleStatus
    sent_at: datetime | None
    error_message: str | None
    created_at: datetime

    model_config = {"from_attributes": True}
