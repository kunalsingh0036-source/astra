"""Payment CRUD endpoints."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from finance.db.engine import get_session
from finance.models.payment import Payment, PaymentStatus
from finance.schemas.payment import PaymentCreate, PaymentOut, PaymentUpdate

router = APIRouter(prefix="/payments", tags=["payments"])


@router.post("/", response_model=PaymentOut, status_code=201)
async def create_payment(
    data: PaymentCreate, session: AsyncSession = Depends(get_session)
):
    payment = Payment(**data.model_dump())
    session.add(payment)
    await session.commit()
    await session.refresh(payment)
    return payment


@router.get("/", response_model=list[PaymentOut])
async def list_payments(
    business_id: uuid.UUID | None = None,
    status: PaymentStatus | None = None,
    invoice_id: uuid.UUID | None = None,
    limit: int = Query(50, le=200),
    offset: int = 0,
    session: AsyncSession = Depends(get_session),
):
    q = select(Payment).order_by(Payment.payment_date.desc())
    if business_id:
        q = q.where(Payment.business_id == business_id)
    if status:
        q = q.where(Payment.status == status)
    if invoice_id:
        q = q.where(Payment.invoice_id == invoice_id)
    q = q.limit(limit).offset(offset)
    result = await session.execute(q)
    return result.scalars().all()


@router.get("/{payment_id}", response_model=PaymentOut)
async def get_payment(
    payment_id: uuid.UUID, session: AsyncSession = Depends(get_session)
):
    payment = await session.get(Payment, payment_id)
    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found")
    return payment


@router.patch("/{payment_id}", response_model=PaymentOut)
async def update_payment(
    payment_id: uuid.UUID,
    data: PaymentUpdate,
    session: AsyncSession = Depends(get_session),
):
    payment = await session.get(Payment, payment_id)
    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found")
    for key, val in data.model_dump(exclude_unset=True).items():
        setattr(payment, key, val)
    await session.commit()
    await session.refresh(payment)
    return payment
