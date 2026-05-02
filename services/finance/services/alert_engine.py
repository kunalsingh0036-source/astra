"""Alert engine — scans financial data and raises alerts.

Why rule-based (not AI):
- Alert conditions are well-defined business rules
- Need to be fast, free, and 100% reliable
- AI would add latency and cost for simple threshold checks
- Rules are auditable and adjustable by the user

Alert types:
- CASH_LOW: Running balance below threshold
- INVOICE_OVERDUE: Unpaid invoices past due date
- PAYMENT_FAILED: Failed payment needing attention
- UNUSUAL_EXPENSE: Expense significantly above category average
- RECURRING_PAYMENT_DUE: Upcoming recurring payment reminder
"""

import uuid
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from finance.models.alert import Alert, AlertSeverity, AlertType
from finance.models.cash_flow import CashFlowSnapshot
from finance.models.expense import Expense
from finance.models.invoice import Invoice, InvoiceStatus
from finance.models.payment import Payment, PaymentStatus

# Thresholds (configurable later)
CASH_LOW_THRESHOLD = Decimal("50000.00")  # ₹50K
EXPENSE_ANOMALY_MULTIPLIER = 3.0  # 3x average = unusual


async def run_alert_scan(
    business_id: uuid.UUID,
    session: AsyncSession,
) -> list[Alert]:
    """Run all alert checks for a business. Returns newly created alerts."""
    alerts: list[Alert] = []

    alerts.extend(await _check_overdue_invoices(business_id, session))
    alerts.extend(await _check_low_cash(business_id, session))
    alerts.extend(await _check_failed_payments(business_id, session))
    alerts.extend(await _check_unusual_expenses(business_id, session))

    if alerts:
        session.add_all(alerts)
        await session.commit()

    return alerts


async def _check_overdue_invoices(
    business_id: uuid.UUID, session: AsyncSession
) -> list[Alert]:
    """Flag unpaid invoices past their due date."""
    q = select(Invoice).where(
        Invoice.business_id == business_id,
        Invoice.status.notin_([InvoiceStatus.PAID, InvoiceStatus.CANCELLED]),
        Invoice.due_date < date.today(),
    )
    overdue = (await session.execute(q)).scalars().all()
    alerts = []

    for inv in overdue:
        # Don't duplicate alerts
        existing = await session.execute(
            select(Alert).where(
                Alert.business_id == business_id,
                Alert.type == AlertType.INVOICE_OVERDUE,
                Alert.related_entity_id == inv.id,
                Alert.is_resolved == False,  # noqa: E712
            )
        )
        if existing.scalar_one_or_none():
            continue

        days_overdue = (date.today() - inv.due_date).days
        severity = AlertSeverity.CRITICAL if days_overdue > 30 else AlertSeverity.WARNING
        balance = inv.total_amount - inv.payment_received

        alerts.append(Alert(
            business_id=business_id,
            type=AlertType.INVOICE_OVERDUE,
            severity=severity,
            title=f"Invoice {inv.invoice_number} overdue by {days_overdue} days",
            message=f"₹{balance:,.2f} due from {inv.counterparty_name}. Due date was {inv.due_date}.",
            related_entity_type="invoice",
            related_entity_id=inv.id,
        ))

    return alerts


async def _check_low_cash(
    business_id: uuid.UUID, session: AsyncSession
) -> list[Alert]:
    """Alert if cash balance drops below threshold."""
    latest_q = (
        select(CashFlowSnapshot)
        .where(CashFlowSnapshot.business_id == business_id)
        .order_by(CashFlowSnapshot.snapshot_date.desc())
        .limit(1)
    )
    latest = (await session.execute(latest_q)).scalar_one_or_none()
    if not latest or latest.running_balance >= CASH_LOW_THRESHOLD:
        return []

    # Don't duplicate
    existing = await session.execute(
        select(Alert).where(
            Alert.business_id == business_id,
            Alert.type == AlertType.CASH_LOW,
            Alert.is_resolved == False,  # noqa: E712
        )
    )
    if existing.scalar_one_or_none():
        return []

    severity = (
        AlertSeverity.CRITICAL
        if latest.running_balance < Decimal("10000.00")
        else AlertSeverity.WARNING
    )

    return [Alert(
        business_id=business_id,
        type=AlertType.CASH_LOW,
        severity=severity,
        title=f"Low cash balance: ₹{latest.running_balance:,.2f}",
        message=f"Cash balance is below ₹{CASH_LOW_THRESHOLD:,.2f}. Review upcoming receivables and payables.",
    )]


async def _check_failed_payments(
    business_id: uuid.UUID, session: AsyncSession
) -> list[Alert]:
    """Alert on payments that failed in the last 7 days."""
    week_ago = date.today() - timedelta(days=7)
    q = select(Payment).where(
        Payment.business_id == business_id,
        Payment.status == PaymentStatus.FAILED,
        Payment.payment_date >= week_ago,
    )
    failed = (await session.execute(q)).scalars().all()
    alerts = []

    for pmt in failed:
        existing = await session.execute(
            select(Alert).where(
                Alert.business_id == business_id,
                Alert.type == AlertType.PAYMENT_FAILED,
                Alert.related_entity_id == pmt.id,
                Alert.is_resolved == False,  # noqa: E712
            )
        )
        if existing.scalar_one_or_none():
            continue

        alerts.append(Alert(
            business_id=business_id,
            type=AlertType.PAYMENT_FAILED,
            severity=AlertSeverity.WARNING,
            title=f"Payment of ₹{pmt.amount:,.2f} failed",
            message=f"Payment via {pmt.payment_mode.value} to {pmt.counterparty_name or 'unknown'} on {pmt.payment_date} failed. Ref: {pmt.reference_number or 'N/A'}.",
            related_entity_type="payment",
            related_entity_id=pmt.id,
        ))

    return alerts


async def _check_unusual_expenses(
    business_id: uuid.UUID, session: AsyncSession
) -> list[Alert]:
    """Flag expenses that are significantly above category average."""
    # Get average per category
    avg_q = select(
        Expense.category,
        func.avg(Expense.amount),
    ).where(
        Expense.business_id == business_id,
    ).group_by(Expense.category)
    averages = dict((await session.execute(avg_q)).all())

    if not averages:
        return []

    # Check recent expenses (last 7 days)
    week_ago = date.today() - timedelta(days=7)
    recent_q = select(Expense).where(
        Expense.business_id == business_id,
        Expense.expense_date >= week_ago,
    )
    recent = (await session.execute(recent_q)).scalars().all()
    alerts = []

    for exp in recent:
        avg = averages.get(exp.category)
        if not avg or avg == 0:
            continue
        if float(exp.amount) > float(avg) * EXPENSE_ANOMALY_MULTIPLIER:
            existing = await session.execute(
                select(Alert).where(
                    Alert.business_id == business_id,
                    Alert.type == AlertType.UNUSUAL_EXPENSE,
                    Alert.related_entity_id == exp.id,
                    Alert.is_resolved == False,  # noqa: E712
                )
            )
            if existing.scalar_one_or_none():
                continue

            alerts.append(Alert(
                business_id=business_id,
                type=AlertType.UNUSUAL_EXPENSE,
                severity=AlertSeverity.INFO,
                title=f"Unusual expense: ₹{exp.amount:,.2f} in {exp.category}",
                message=f"This expense to {exp.vendor_name} is {float(exp.amount)/float(avg):.1f}x the average for {exp.category} (avg ₹{float(avg):,.2f}).",
                related_entity_type="expense",
                related_entity_id=exp.id,
            ))

    return alerts
