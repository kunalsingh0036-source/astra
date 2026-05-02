"""Cash flow snapshot engine — generates daily snapshots and AI forecasts.

Why daily snapshots (not real-time):
- Cash flow is a daily metric — intraday granularity adds noise, not signal
- Snapshots are fast to query for charts and dashboards
- Forecasts only make sense at daily+ granularity

Why Claude Sonnet for forecasting (not statistical models):
- Time series models (ARIMA, Prophet) need months of clean historical data
- We're starting fresh — no historical data yet
- Claude can reason about upcoming invoices, seasonal patterns, and known expenses
- When we have 6+ months of data, we can add Prophet as a secondary signal

Snapshot generation:
- Aggregates payments (inflows) and expenses (outflows) per day per business
- Calculates running balance from bank account totals + cumulative net flows
- Stores forecasts from AI analysis
"""

import uuid
from datetime import date, timedelta
from decimal import Decimal

import anthropic
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from finance.config import settings
from finance.models.bank_account import BankAccount
from finance.models.cash_flow import CashFlowSnapshot
from finance.models.expense import Expense
from finance.models.invoice import Invoice, InvoiceStatus, InvoiceType
from finance.models.payment import Payment, PaymentStatus


async def generate_daily_snapshot(
    business_id: uuid.UUID,
    snapshot_date: date,
    session: AsyncSession,
    generate_forecast: bool = True,
) -> CashFlowSnapshot:
    """Generate a cash flow snapshot for a specific business and date.

    Aggregates confirmed payments (inflows) and expenses (outflows),
    calculates running balance, and optionally generates AI forecasts.
    """
    # Check if snapshot already exists
    existing = await session.execute(
        select(CashFlowSnapshot).where(
            CashFlowSnapshot.business_id == business_id,
            CashFlowSnapshot.snapshot_date == snapshot_date,
        )
    )
    if existing.scalar_one_or_none():
        raise ValueError(f"Snapshot already exists for {snapshot_date}")

    # Calculate inflows — confirmed payments received
    inflow_q = select(func.coalesce(func.sum(Payment.amount), 0)).where(
        Payment.business_id == business_id,
        Payment.payment_date == snapshot_date,
        Payment.status == PaymentStatus.CONFIRMED,
    )
    inflow = (await session.execute(inflow_q)).scalar() or Decimal("0.00")

    # Calculate outflows — expenses for the day
    outflow_q = select(func.coalesce(func.sum(Expense.amount), 0)).where(
        Expense.business_id == business_id,
        Expense.expense_date == snapshot_date,
    )
    outflow = (await session.execute(outflow_q)).scalar() or Decimal("0.00")

    net_flow = inflow - outflow

    # Running balance = previous day's running balance + today's net flow
    prev_q = (
        select(CashFlowSnapshot.running_balance)
        .where(
            CashFlowSnapshot.business_id == business_id,
            CashFlowSnapshot.snapshot_date < snapshot_date,
        )
        .order_by(CashFlowSnapshot.snapshot_date.desc())
        .limit(1)
    )
    prev_balance = (await session.execute(prev_q)).scalar()

    if prev_balance is None:
        # First snapshot — use current bank balance as starting point
        bank_q = select(func.coalesce(func.sum(BankAccount.current_balance), 0)).where(
            BankAccount.business_id == business_id
        )
        prev_balance = (await session.execute(bank_q)).scalar() or Decimal("0.00")

    running_balance = prev_balance + net_flow

    # Generate forecasts
    forecast_30d = None
    forecast_60d = None
    forecast_90d = None

    if generate_forecast:
        forecasts = await _generate_forecasts(business_id, running_balance, session)
        forecast_30d = forecasts.get("30d")
        forecast_60d = forecasts.get("60d")
        forecast_90d = forecasts.get("90d")

    snapshot = CashFlowSnapshot(
        business_id=business_id,
        snapshot_date=snapshot_date,
        inflow=inflow,
        outflow=outflow,
        net_flow=net_flow,
        running_balance=running_balance,
        forecast_30d=forecast_30d,
        forecast_60d=forecast_60d,
        forecast_90d=forecast_90d,
    )
    session.add(snapshot)
    await session.commit()
    await session.refresh(snapshot)
    return snapshot


async def generate_all_snapshots(
    snapshot_date: date,
    session: AsyncSession,
) -> list[CashFlowSnapshot]:
    """Generate snapshots for all businesses."""
    from finance.models.business import Business

    businesses = (await session.execute(select(Business))).scalars().all()
    snapshots = []
    for biz in businesses:
        try:
            snap = await generate_daily_snapshot(biz.id, snapshot_date, session)
            snapshots.append(snap)
        except ValueError:
            pass  # Already exists
    return snapshots


async def _generate_forecasts(
    business_id: uuid.UUID,
    current_balance: Decimal,
    session: AsyncSession,
) -> dict[str, Decimal | None]:
    """Use Claude Sonnet to forecast cash position at 30/60/90 days.

    Gathers context about upcoming invoices, recurring expenses, and historical patterns,
    then asks Claude to estimate future balances.
    """
    # Gather context
    today = date.today()

    # Upcoming receivables (unpaid invoices due in next 90 days)
    recv_q = select(
        func.sum(Invoice.total_amount - Invoice.payment_received)
    ).where(
        Invoice.business_id == business_id,
        Invoice.type == InvoiceType.RECEIVABLE,
        Invoice.status.notin_([InvoiceStatus.PAID, InvoiceStatus.CANCELLED]),
        Invoice.due_date <= today + timedelta(days=90),
    )
    upcoming_receivable = (await session.execute(recv_q)).scalar() or Decimal("0.00")

    # Upcoming payables
    pay_q = select(
        func.sum(Invoice.total_amount - Invoice.payment_received)
    ).where(
        Invoice.business_id == business_id,
        Invoice.type == InvoiceType.PAYABLE,
        Invoice.status.notin_([InvoiceStatus.PAID, InvoiceStatus.CANCELLED]),
        Invoice.due_date <= today + timedelta(days=90),
    )
    upcoming_payable = (await session.execute(pay_q)).scalar() or Decimal("0.00")

    # Monthly recurring expenses
    recurring_q = select(func.sum(Expense.amount)).where(
        Expense.business_id == business_id,
        Expense.is_recurring == True,  # noqa: E712
        Expense.recurrence_frequency == "monthly",
    )
    monthly_recurring = (await session.execute(recurring_q)).scalar() or Decimal("0.00")

    prompt = f"""Based on this financial data, estimate the cash balance at 30, 60, and 90 days from now.

Current balance: ₹{current_balance:,.2f}
Upcoming receivables (next 90 days): ₹{upcoming_receivable:,.2f}
Upcoming payables (next 90 days): ₹{upcoming_payable:,.2f}
Monthly recurring expenses: ₹{monthly_recurring:,.2f}

Assume receivables come in gradually and payables are paid on time.
Respond with ONLY three numbers, one per line, no currency symbols or commas:
30d_balance
60d_balance
90d_balance"""

    try:
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        response = await client.messages.create(
            model=settings.model_haiku,  # Use Haiku for cost — forecasting context is small
            max_tokens=100,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        lines = [l.strip() for l in text.split("\n") if l.strip()]

        if len(lines) >= 3:
            return {
                "30d": Decimal(lines[0].replace(",", "")),
                "60d": Decimal(lines[1].replace(",", "")),
                "90d": Decimal(lines[2].replace(",", "")),
            }
    except Exception:
        pass

    return {"30d": None, "60d": None, "90d": None}
