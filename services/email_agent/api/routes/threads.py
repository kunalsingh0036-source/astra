"""Email thread endpoints."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from email_agent.db.engine import get_session
from email_agent.models.thread import EmailThread, ThreadPriority
from email_agent.schemas.thread import ThreadDetail, ThreadOut

router = APIRouter(prefix="/threads", tags=["threads"])


@router.get("/", response_model=list[ThreadOut])
async def list_threads(
    account_id: uuid.UUID | None = None,
    priority: ThreadPriority | None = None,
    needs_response: bool | None = None,
    category: str | None = None,
    limit: int = Query(50, le=200),
    offset: int = 0,
    session: AsyncSession = Depends(get_session),
):
    q = select(EmailThread).order_by(EmailThread.last_message_at.desc())
    if account_id:
        q = q.where(EmailThread.account_id == account_id)
    if priority:
        q = q.where(EmailThread.ai_priority == priority)
    if needs_response is not None:
        q = q.where(EmailThread.needs_response == needs_response)
    if category:
        q = q.where(EmailThread.ai_category == category)
    q = q.limit(limit).offset(offset)
    result = await session.execute(q)
    return result.scalars().all()


@router.get("/{thread_id}", response_model=ThreadDetail)
async def get_thread(
    thread_id: uuid.UUID, session: AsyncSession = Depends(get_session)
):
    result = await session.execute(
        select(EmailThread)
        .where(EmailThread.id == thread_id)
        .options(selectinload(EmailThread.messages))
    )
    thread = result.scalar_one_or_none()
    if not thread:
        raise HTTPException(status_code=404, detail="Thread not found")
    return thread
