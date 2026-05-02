"""Email message schemas."""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from email_agent.models.email_message import EmailDirection


class MessageOut(BaseModel):
    id: uuid.UUID
    account_id: uuid.UUID
    thread_id: uuid.UUID | None
    gmail_message_id: str
    gmail_thread_id: str
    direction: EmailDirection
    from_address: str
    to_addresses: list[str]
    cc_addresses: list[str]
    subject: str
    body_text: str | None
    snippet: str | None
    sent_at: datetime
    is_read: bool
    is_starred: bool
    has_attachments: bool
    gmail_labels: list[str]
    ai_category: str | None
    ai_priority: str | None
    ai_summary: str | None
    ai_action_needed: bool | None
    created_at: datetime

    model_config = {"from_attributes": True}


class MessageSummary(BaseModel):
    total: int
    unread: int
    action_needed: int
    by_category: dict[str, int]


class SendRequest(BaseModel):
    to: list[str]
    cc: list[str] = Field(default_factory=list)
    bcc: list[str] = Field(default_factory=list)
    subject: str
    body: str
    reply_to_message_id: uuid.UUID | None = None
