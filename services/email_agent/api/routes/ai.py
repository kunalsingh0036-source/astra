"""AI endpoints — classify emails, generate drafts."""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from email_agent.db.engine import get_session
from email_agent.models.email_message import EmailMessage
from email_agent.services.classifier import ClassificationResult, classify_email

router = APIRouter(prefix="/ai", tags=["ai"])


class ClassifyRequest(BaseModel):
    from_address: str
    to_addresses: list[str]
    subject: str
    body_text: str | None = None


class ClassifyResponse(BaseModel):
    category: str
    priority: str
    summary: str
    action_needed: bool


@router.post("/classify", response_model=ClassifyResponse)
async def classify(data: ClassifyRequest):
    """Classify an email's category, priority, and action needed."""
    result = await classify_email(
        from_address=data.from_address,
        to_addresses=data.to_addresses,
        subject=data.subject,
        body_text=data.body_text,
    )
    return ClassifyResponse(
        category=result.category,
        priority=result.priority,
        summary=result.summary,
        action_needed=result.action_needed,
    )


@router.post("/classify/{message_id}", response_model=ClassifyResponse)
async def classify_existing(
    message_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
):
    """Classify an existing message and update it in the database."""
    msg = await session.get(EmailMessage, message_id)
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")

    result = await classify_email(
        from_address=msg.from_address,
        to_addresses=msg.to_addresses,
        subject=msg.subject,
        body_text=msg.body_text,
        snippet=msg.snippet,
    )

    msg.ai_category = result.category
    msg.ai_priority = result.priority
    msg.ai_summary = result.summary
    msg.ai_action_needed = result.action_needed
    await session.commit()

    return ClassifyResponse(
        category=result.category,
        priority=result.priority,
        summary=result.summary,
        action_needed=result.action_needed,
    )
