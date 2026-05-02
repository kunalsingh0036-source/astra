"""Cash flow endpoints."""

import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from finance.db.engine import get_session
from finance.models.cash_flow import CashFlowSnapshot
from finance.schemas.cash_flow import CashFlowSnapshotOut, CashFlowSummary

router = APIRouter(prefix="/cash-flow", tags=["cash-flow"])


@router.get("/", response_model=list[CashFlowSnapshotOut])
async def list_snapshots(
    business_id: uuid.UUID | None = None,
    limit: int = Query(30, le=365),
    session: AsyncSession = Depends(get_session),
):
    q = select(CashFlowSnapshot).order_by(CashFlowSnapshot.snapshot_date.desc())
    if business_id:
        q = q.where(CashFlowSnapshot.business_id == business_id)
    q = q.limit(limit)
    result = await session.execute(q)
    return result.scalars().all()


@router.get("/summary", response_model=CashFlowSummary)
async def cash_flow_summary(
    business_id: uuid.UUID | None = None,
    session: AsyncSession = Depends(get_session),
):
    from datetime import date, timedelta

    thirty_days_ago = date.today() - timedelta(days=30)

    base_filter = [CashFlowSnapshot.snapshot_date >= thirty_days_ago]
    if business_id:
        base_filter.append(CashFlowSnapshot.business_id == business_id)

    # Aggregate last 30 days
    agg_q = select(
        func.coalesce(func.sum(CashFlowSnapshot.inflow), 0),
        func.coalesce(func.sum(CashFlowSnapshot.outflow), 0),
        func.coalesce(func.sum(CashFlowSnapshot.net_flow), 0),
    ).where(*base_filter)
    agg = (await session.execute(agg_q)).one()

    # Latest snapshot for current balance and forecasts
    latest_q = select(CashFlowSnapshot).order_by(
        CashFlowSnapshot.snapshot_date.desc()
    )
    if business_id:
        latest_q = latest_q.where(CashFlowSnapshot.business_id == business_id)
    latest_q = latest_q.limit(1)
    latest = (await session.execute(latest_q)).scalar_one_or_none()

    return CashFlowSummary(
        current_balance=latest.running_balance if latest else Decimal("0.00"),
        inflow_30d=agg[0],
        outflow_30d=agg[1],
        net_flow_30d=agg[2],
        forecast_30d=latest.forecast_30d if latest else None,
        forecast_60d=latest.forecast_60d if latest else None,
        forecast_90d=latest.forecast_90d if latest else None,
    )
