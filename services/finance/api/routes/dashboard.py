"""Dashboard — single aggregated endpoint for Astra."""

import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from finance.db.engine import get_session
from finance.models.alert import Alert
from finance.models.bank_account import BankAccount
from finance.schemas.alert import AlertOut
from finance.schemas.dashboard import DashboardData

from finance.api.routes.invoices import invoice_summary
from finance.api.routes.expenses import expense_summary
from finance.api.routes.cash_flow import cash_flow_summary

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/", response_model=DashboardData)
async def get_dashboard(
    business_id: uuid.UUID | None = None,
    session: AsyncSession = Depends(get_session),
):
    """Cross-business financial snapshot — the single API call Astra uses."""

    inv_sum = await invoice_summary(business_id=business_id, session=session)
    exp_sum = await expense_summary(business_id=business_id, session=session)
    cf_sum = await cash_flow_summary(business_id=business_id, session=session)

    # Recent unresolved alerts
    alert_q = (
        select(Alert)
        .where(Alert.is_resolved == False)  # noqa: E712
        .order_by(Alert.created_at.desc())
        .limit(10)
    )
    if business_id:
        alert_q = alert_q.where(Alert.business_id == business_id)
    alerts = (await session.execute(alert_q)).scalars().all()

    # Total bank balance
    bal_q = select(func.coalesce(func.sum(BankAccount.current_balance), 0))
    if business_id:
        bal_q = bal_q.where(BankAccount.business_id == business_id)
    total_balance = (await session.execute(bal_q)).scalar() or Decimal("0.00")

    return DashboardData(
        invoice_summary=inv_sum,
        expense_summary=exp_sum,
        cash_flow=cf_sum,
        recent_alerts=[AlertOut.model_validate(a) for a in alerts],
        total_bank_balance=total_balance,
    )
