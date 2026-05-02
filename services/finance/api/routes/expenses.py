"""Expense CRUD + summary endpoints."""

import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from finance.db.engine import get_session
from finance.models.expense import Expense
from finance.schemas.expense import ExpenseCreate, ExpenseOut, ExpenseSummary, ExpenseUpdate

router = APIRouter(prefix="/expenses", tags=["expenses"])


@router.post("/", response_model=ExpenseOut, status_code=201)
async def create_expense(
    data: ExpenseCreate, session: AsyncSession = Depends(get_session)
):
    expense = Expense(**data.model_dump())
    session.add(expense)
    await session.commit()
    await session.refresh(expense)
    return expense


@router.get("/", response_model=list[ExpenseOut])
async def list_expenses(
    business_id: uuid.UUID | None = None,
    category: str | None = None,
    limit: int = Query(50, le=200),
    offset: int = 0,
    session: AsyncSession = Depends(get_session),
):
    q = select(Expense).order_by(Expense.expense_date.desc())
    if business_id:
        q = q.where(Expense.business_id == business_id)
    if category:
        q = q.where(Expense.category == category)
    q = q.limit(limit).offset(offset)
    result = await session.execute(q)
    return result.scalars().all()


@router.get("/summary", response_model=ExpenseSummary)
async def expense_summary(
    business_id: uuid.UUID | None = None,
    session: AsyncSession = Depends(get_session),
):
    base_filter = []
    if business_id:
        base_filter.append(Expense.business_id == business_id)

    # Total amount
    total_q = select(func.coalesce(func.sum(Expense.amount), 0)).where(*base_filter)
    total_amount = (await session.execute(total_q)).scalar() or Decimal("0.00")

    # By category
    cat_q = select(Expense.category, func.sum(Expense.amount)).where(*base_filter).group_by(Expense.category)
    cat_result = (await session.execute(cat_q)).all()
    by_category = {row[0]: row[1] for row in cat_result}

    # Recurring monthly
    recurring_q = select(func.coalesce(func.sum(Expense.amount), 0)).where(
        Expense.is_recurring == True,  # noqa: E712
        Expense.recurrence_frequency == "monthly",
        *base_filter,
    )
    recurring_monthly = (await session.execute(recurring_q)).scalar() or Decimal("0.00")

    return ExpenseSummary(
        total_amount=total_amount,
        by_category=by_category,
        recurring_monthly=recurring_monthly,
    )


@router.get("/{expense_id}", response_model=ExpenseOut)
async def get_expense(
    expense_id: uuid.UUID, session: AsyncSession = Depends(get_session)
):
    expense = await session.get(Expense, expense_id)
    if not expense:
        raise HTTPException(status_code=404, detail="Expense not found")
    return expense


@router.patch("/{expense_id}", response_model=ExpenseOut)
async def update_expense(
    expense_id: uuid.UUID,
    data: ExpenseUpdate,
    session: AsyncSession = Depends(get_session),
):
    expense = await session.get(Expense, expense_id)
    if not expense:
        raise HTTPException(status_code=404, detail="Expense not found")
    for key, val in data.model_dump(exclude_unset=True).items():
        setattr(expense, key, val)
    await session.commit()
    await session.refresh(expense)
    return expense
