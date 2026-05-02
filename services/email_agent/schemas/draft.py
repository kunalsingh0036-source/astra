"""Draft schemas."""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from email_agent.models.draft import DraftStatus


class DraftCreateRequest(BaseModel):
    """Request to generate an AI draft."""
    account_id: uuid.UUID
    to: list[str]
    cc: list[str] = Field(default_factory=list)
    subject: str | None = None  # If None, AI generates subject
    intent: str  # What the user wants to say
    tone: str = "professional"  # professional, casual, friendly, firm
    reply_to_message_id: uuid.UUID | None = None
    template_id: uuid.UUID | None = None


class DraftUpdate(BaseModel):
    subject: str | None = None
    body_text: str | None = None
    to_addresses: list[str] | None = None
    cc_addresses: list[str] | None = None
    status: DraftStatus | None = None


class DraftOut(BaseModel):
    id: uuid.UUID
    account_id: uuid.UUID
    reply_to_message_id: uuid.UUID | None
    to_addresses: list[str]
    cc_addresses: list[str]
    subject: str
    body_text: str
    body_html: str | None
    status: DraftStatus
    tone: str | None
    gmail_draft_id: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
