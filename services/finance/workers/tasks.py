"""Celery tasks for Finance Agent.

All tasks bridge sync Celery → async SQLAlchemy via asyncio.run().
"""

import asyncio
from datetime import date

from finance.workers.celery_app import app


def _run_async(coro):
    """Run an async coroutine from sync Celery task."""
    return asyncio.run(coro)


async def _get_session():
    """Create a one-off async session for background tasks."""
    from finance.db.engine import async_session
    return async_session()


@app.task(name="finance.workers.tasks.daily_snapshot")
def daily_snapshot():
    """Generate daily cash flow snapshots for all businesses."""
    return _run_async(_daily_snapshot_impl())


async def _daily_snapshot_impl():
    from finance.services.cash_flow_engine import generate_all_snapshots

    session = await _get_session()
    async with session:
        snapshots = await generate_all_snapshots(date.today(), session)
        return {
            "date": str(date.today()),
            "snapshots_created": len(snapshots),
        }


@app.task(name="finance.workers.tasks.daily_alert_scan")
def daily_alert_scan():
    """Run alert checks for all businesses."""
    return _run_async(_daily_alert_scan_impl())


async def _daily_alert_scan_impl():
    from sqlalchemy import select

    from finance.models.business import Business
    from finance.services.alert_engine import run_alert_scan

    session = await _get_session()
    async with session:
        businesses = (await session.execute(select(Business))).scalars().all()
        total_alerts = 0
        for biz in businesses:
            alerts = await run_alert_scan(biz.id, session)
            total_alerts += len(alerts)
        return {
            "businesses_scanned": len(businesses),
            "new_alerts": total_alerts,
        }


@app.task(name="finance.workers.tasks.check_overdue_invoices")
def check_overdue_invoices():
    """Mark overdue invoices and generate alerts."""
    return _run_async(_check_overdue_impl())


async def _check_overdue_impl():
    from sqlalchemy import select

    from finance.models.invoice import Invoice, InvoiceStatus

    session = await _get_session()
    async with session:
        q = select(Invoice).where(
            Invoice.status.notin_([InvoiceStatus.PAID, InvoiceStatus.CANCELLED, InvoiceStatus.OVERDUE]),
            Invoice.due_date < date.today(),
        )
        overdue = (await session.execute(q)).scalars().all()
        for inv in overdue:
            inv.status = InvoiceStatus.OVERDUE
        await session.commit()
        return {"marked_overdue": len(overdue)}


@app.task(name="finance.workers.tasks.categorize_uncategorized")
def categorize_uncategorized():
    """Auto-categorize expenses that haven't been AI-classified."""
    return _run_async(_categorize_impl())


async def _categorize_impl():
    from sqlalchemy import select

    from finance.models.expense import Expense
    from finance.services.expense_categorizer import categorize_expense

    session = await _get_session()
    async with session:
        q = select(Expense).where(
            Expense.ai_categorized == False,  # noqa: E712
        ).limit(50)  # Batch limit to control API costs
        expenses = (await session.execute(q)).scalars().all()

        categorized = 0
        for exp in expenses:
            result = await categorize_expense(
                vendor_name=exp.vendor_name,
                amount=float(exp.amount),
                description=exp.description,
            )
            if result.confidence > 0:
                exp.category = result.category
                exp.subcategory = result.subcategory
                exp.ai_categorized = True
                exp.ai_confidence = result.confidence
                categorized += 1

        await session.commit()
        return {"categorized": categorized, "total_checked": len(expenses)}
