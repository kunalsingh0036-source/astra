"""Alert endpoints — read and manage financial alerts."""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from finance.db.engine import get_session
from finance.models.alert import Alert, AlertSeverity
from finance.schemas.alert import AlertOut, AlertUpdate

router = APIRouter(prefix="/alerts", tags=["alerts"])


@router.get("/", response_model=list[AlertOut])
async def list_alerts(
    business_id: uuid.UUID | None = None,
    severity: AlertSeverity | None = None,
    unread_only: bool = False,
    unresolved_only: bool = False,
    limit: int = Query(50, le=200),
    session: AsyncSession = Depends(get_session),
):
    q = select(Alert).order_by(Alert.created_at.desc())
    if business_id:
        q = q.where(Alert.business_id == business_id)
    if severity:
        q = q.where(Alert.severity == severity)
    if unread_only:
        q = q.where(Alert.is_read == False)  # noqa: E712
    if unresolved_only:
        q = q.where(Alert.is_resolved == False)  # noqa: E712
    q = q.limit(limit)
    result = await session.execute(q)
    return result.scalars().all()


@router.patch("/{alert_id}", response_model=AlertOut)
async def update_alert(
    alert_id: uuid.UUID,
    data: AlertUpdate,
    session: AsyncSession = Depends(get_session),
):
    alert = await session.get(Alert, alert_id)
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    updates = data.model_dump(exclude_unset=True)
    if updates.get("is_resolved") and not alert.is_resolved:
        updates["resolved_at"] = datetime.now(timezone.utc)
    for key, val in updates.items():
        setattr(alert, key, val)
    await session.commit()
    await session.refresh(alert)
    return alert


@router.post("/{alert_id}/read", response_model=AlertOut)
async def mark_alert_read(
    alert_id: uuid.UUID, session: AsyncSession = Depends(get_session)
):
    alert = await session.get(Alert, alert_id)
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    alert.is_read = True
    await session.commit()
    await session.refresh(alert)
    return alert
