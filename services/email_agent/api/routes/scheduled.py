"""Scheduled email endpoints."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from email_agent.db.engine import get_session
from email_agent.models.scheduled import ScheduleStatus, ScheduledEmail
from email_agent.schemas.scheduled import ScheduleCreate, ScheduledOut

router = APIRouter(prefix="/scheduled", tags=["scheduled"])


@router.post("/", response_model=ScheduledOut, status_code=201)
async def schedule_email(
    data: ScheduleCreate, session: AsyncSession = Depends(get_session)
):
    scheduled = ScheduledEmail(**data.model_dump())
    session.add(scheduled)
    await session.commit()
    await session.refresh(scheduled)
    return scheduled


@router.get("/", response_model=list[ScheduledOut])
async def list_scheduled(
    status: ScheduleStatus | None = None,
    limit: int = Query(20, le=100),
    session: AsyncSession = Depends(get_session),
):
    q = select(ScheduledEmail).order_by(ScheduledEmail.scheduled_for)
    if status:
        q = q.where(ScheduledEmail.status == status)
    q = q.limit(limit)
    result = await session.execute(q)
    return result.scalars().all()


@router.delete("/{schedule_id}")
async def cancel_scheduled(
    schedule_id: uuid.UUID, session: AsyncSession = Depends(get_session)
):
    scheduled = await session.get(ScheduledEmail, schedule_id)
    if not scheduled:
        raise HTTPException(status_code=404, detail="Scheduled email not found")
    if scheduled.status != ScheduleStatus.PENDING:
        raise HTTPException(status_code=400, detail=f"Cannot cancel — status is {scheduled.status.value}")
    scheduled.status = ScheduleStatus.CANCELLED
    await session.commit()
    return {"status": "cancelled", "id": str(schedule_id)}
