"""AI-powered endpoints — expense categorization, payment matching, alert scanning."""

import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from finance.db.engine import get_session
from finance.models.expense import Expense
from finance.schemas.alert import AlertOut
from finance.services.alert_engine import run_alert_scan
from finance.services.expense_categorizer import CategorizationResult, categorize_expense
from finance.services.payment_matcher import MatchResult as MatchResultModel, auto_match_payments

router = APIRouter(prefix="/ai", tags=["ai"])


class CategorizeRequest(BaseModel):
    vendor_name: str
    amount: float
    description: str | None = None


class MatchResultOut(BaseModel):
    payment_id: uuid.UUID
    invoice_id: uuid.UUID
    confidence: float
    match_type: str


class MatchResponse(BaseModel):
    matched: int
    results: list[MatchResultOut]


@router.post("/categorize", response_model=CategorizationResult)
async def categorize(data: CategorizeRequest):
    """Classify an expense using Claude Haiku."""
    return await categorize_expense(
        vendor_name=data.vendor_name,
        amount=data.amount,
        description=data.description,
    )


@router.post("/categorize-and-save")
async def categorize_and_save(
    expense_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
):
    """Categorize an existing expense and update it in the database."""
    expense = await session.get(Expense, expense_id)
    if not expense:
        return {"error": "Expense not found"}

    result = await categorize_expense(
        vendor_name=expense.vendor_name,
        amount=float(expense.amount),
        description=expense.description,
    )
    expense.category = result.category
    expense.subcategory = result.subcategory
    expense.ai_categorized = True
    expense.ai_confidence = result.confidence
    await session.commit()

    return {"expense_id": str(expense_id), "categorization": result}


@router.post("/match-payments/{business_id}", response_model=MatchResponse)
async def match_payments(
    business_id: uuid.UUID,
    apply: bool = False,
    session: AsyncSession = Depends(get_session),
):
    """Find and optionally apply payment-to-invoice matches."""
    matches = await auto_match_payments(business_id, session, apply=apply)
    return MatchResponse(
        matched=len(matches),
        results=[
            MatchResultOut(
                payment_id=m.payment_id,
                invoice_id=m.invoice_id,
                confidence=m.confidence,
                match_type=m.match_type,
            )
            for m in matches
        ],
    )


@router.post("/scan-alerts/{business_id}", response_model=list[AlertOut])
async def scan_alerts(
    business_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
):
    """Run all alert checks for a business."""
    alerts = await run_alert_scan(business_id, session)
    return [AlertOut.model_validate(a) for a in alerts]
