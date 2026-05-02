"""Email message endpoints."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from email_agent.db.engine import get_session
from email_agent.models.email_message import EmailDirection, EmailMessage
from email_agent.schemas.message import MessageOut, MessageSummary, SendRequest
from email_agent.services.gmail_client import modify_labels, send_email

router = APIRouter(prefix="/messages", tags=["messages"])


@router.get("/", response_model=list[MessageOut])
async def list_messages(
    account_id: uuid.UUID | None = None,
    direction: EmailDirection | None = None,
    category: str | None = None,
    unread_only: bool = False,
    action_needed_only: bool = False,
    limit: int = Query(50, le=200),
    offset: int = 0,
    session: AsyncSession = Depends(get_session),
):
    q = select(EmailMessage).order_by(EmailMessage.sent_at.desc())
    if account_id:
        q = q.where(EmailMessage.account_id == account_id)
    if direction:
        q = q.where(EmailMessage.direction == direction)
    if category:
        q = q.where(EmailMessage.ai_category == category)
    if unread_only:
        q = q.where(EmailMessage.is_read == False)  # noqa: E712
    if action_needed_only:
        q = q.where(EmailMessage.ai_action_needed == True)  # noqa: E712
    q = q.limit(limit).offset(offset)
    result = await session.execute(q)
    return result.scalars().all()


@router.get("/summary", response_model=MessageSummary)
async def message_summary(
    account_id: uuid.UUID | None = None,
    session: AsyncSession = Depends(get_session),
):
    base_filter = []
    if account_id:
        base_filter.append(EmailMessage.account_id == account_id)

    total = (await session.execute(
        select(func.count()).select_from(EmailMessage).where(*base_filter)
    )).scalar() or 0

    unread = (await session.execute(
        select(func.count()).select_from(EmailMessage).where(
            EmailMessage.is_read == False, *base_filter  # noqa: E712
        )
    )).scalar() or 0

    action_needed = (await session.execute(
        select(func.count()).select_from(EmailMessage).where(
            EmailMessage.ai_action_needed == True, *base_filter  # noqa: E712
        )
    )).scalar() or 0

    cat_q = select(
        EmailMessage.ai_category, func.count()
    ).where(*base_filter).group_by(EmailMessage.ai_category)
    cat_result = (await session.execute(cat_q)).all()
    by_category = {row[0] or "unclassified": row[1] for row in cat_result}

    return MessageSummary(
        total=total,
        unread=unread,
        action_needed=action_needed,
        by_category=by_category,
    )


@router.get("/{message_id}", response_model=MessageOut)
async def get_message(
    message_id: uuid.UUID, session: AsyncSession = Depends(get_session)
):
    msg = await session.get(EmailMessage, message_id)
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")
    return msg


@router.post("/send")
async def send(data: SendRequest, session: AsyncSession = Depends(get_session)):
    """Send an email via Gmail API."""
    result = await send_email(
        to=data.to,
        subject=data.subject,
        body=data.body,
        cc=data.cc,
        bcc=data.bcc,
    )
    if result is None:
        raise HTTPException(
            status_code=503,
            detail="Gmail API not configured. Set up OAuth2 credentials first.",
        )
    return {"status": "sent", "gmail_id": result.get("id"), "thread_id": result.get("threadId")}


async def _get_gmail_id(
    message_id: uuid.UUID, session: AsyncSession
) -> tuple[EmailMessage, str]:
    msg = await session.get(EmailMessage, message_id)
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")
    if not msg.gmail_message_id:
        raise HTTPException(status_code=400, detail="Message has no gmail_message_id")
    return msg, msg.gmail_message_id


@router.post("/{message_id}/archive")
async def archive(
    message_id: uuid.UUID, session: AsyncSession = Depends(get_session)
):
    """Archive a message (remove it from the INBOX label)."""
    msg, gmail_id = await _get_gmail_id(message_id, session)
    try:
        result = await modify_labels(gmail_id, remove=["INBOX"])
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Gmail API error: {e}")
    if result is None:
        raise HTTPException(status_code=503, detail="Gmail not configured")
    # Mirror the label change locally so lists reflect state immediately.
    labels = list(msg.gmail_labels or [])
    msg.gmail_labels = [l for l in labels if l != "INBOX"]
    await session.commit()
    return {"status": "archived", "message_id": str(msg.id)}


@router.post("/{message_id}/star")
async def star(
    message_id: uuid.UUID,
    starred: bool = Query(True),
    session: AsyncSession = Depends(get_session),
):
    """Star or unstar a message."""
    msg, gmail_id = await _get_gmail_id(message_id, session)
    try:
        result = await modify_labels(
            gmail_id,
            add=["STARRED"] if starred else None,
            remove=None if starred else ["STARRED"],
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Gmail API error: {e}")
    if result is None:
        raise HTTPException(status_code=503, detail="Gmail not configured")
    labels = list(msg.gmail_labels or [])
    if starred and "STARRED" not in labels:
        labels.append("STARRED")
    if not starred:
        labels = [l for l in labels if l != "STARRED"]
    msg.gmail_labels = labels
    msg.is_starred = starred
    await session.commit()
    return {"status": "starred" if starred else "unstarred", "message_id": str(msg.id)}


@router.post("/{message_id}/mark_read")
async def mark_read(
    message_id: uuid.UUID, session: AsyncSession = Depends(get_session)
):
    """Mark a message read (remove the UNREAD label)."""
    msg, gmail_id = await _get_gmail_id(message_id, session)
    try:
        result = await modify_labels(gmail_id, remove=["UNREAD"])
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Gmail API error: {e}")
    if result is None:
        raise HTTPException(status_code=503, detail="Gmail not configured")
    labels = list(msg.gmail_labels or [])
    msg.gmail_labels = [l for l in labels if l != "UNREAD"]
    msg.is_read = True
    await session.commit()
    return {"status": "read", "message_id": str(msg.id)}
