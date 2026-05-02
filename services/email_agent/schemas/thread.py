"""Email thread schemas."""

import uuid
from datetime import datetime

from pydantic import BaseModel

from email_agent.models.thread import ThreadPriority
from email_agent.schemas.message import MessageOut


class ThreadOut(BaseModel):
    id: uuid.UUID
    account_id: uuid.UUID
    gmail_thread_id: str
    subject: str
    participants: list[str]
    message_count: int
    first_message_at: datetime | None
    last_message_at: datetime | None
    ai_priority: ThreadPriority
    ai_summary: str | None
    ai_category: str | None
    needs_response: bool | None
    created_at: datetime

    model_config = {"from_attributes": True}


class ThreadDetail(ThreadOut):
    messages: list[MessageOut] = []
