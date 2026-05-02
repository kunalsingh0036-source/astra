"""Invoice CRUD + summary endpoints."""

import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from finance.db.engine import get_session
from finance.models.invoice import Invoice, InvoiceStatus, InvoiceType
from finance.schemas.invoice import (
    InvoiceCreate,
    InvoiceOut,
    InvoiceSummary,
    InvoiceUpdate,
)

router = APIRouter(prefix="/invoices", tags=["invoices"])


@router.post("/", response_model=InvoiceOut, status_code=201)
async def create_invoice(
    data: InvoiceCreate, session: AsyncSession = Depends(get_session)
):
    invoice = Invoice(**data.model_dump())
    session.add(invoice)
    await session.commit()
    await session.refresh(invoice)
    return invoice


@router.get("/", response_model=list[InvoiceOut])
async def list_invoices(
    business_id: uuid.UUID | None = None,
    status: InvoiceStatus | None = None,
    type: InvoiceType | None = None,
    limit: int = Query(50, le=200),
    offset: int = 0,
    session: AsyncSession = Depends(get_session),
):
    q = select(Invoice).order_by(Invoice.due_date.desc())
    if business_id:
        q = q.where(Invoice.business_id == business_id)
    if status:
        q = q.where(Invoice.status == status)
    if type:
        q = q.where(Invoice.type == type)
    q = q.limit(limit).offset(offset)
    result = await session.execute(q)
    return result.scalars().all()


@router.get("/summary", response_model=InvoiceSummary)
async def invoice_summary(
    business_id: uuid.UUID | None = None,
    session: AsyncSession = Depends(get_session),
):
    base = select(Invoice).where(
        Invoice.status.notin_([InvoiceStatus.CANCELLED, InvoiceStatus.PAID])
    )
    if business_id:
        base = base.where(Invoice.business_id == business_id)

    # Total receivable
    recv_q = select(func.coalesce(func.sum(Invoice.total_amount - Invoice.payment_received), 0)).where(
        Invoice.type == InvoiceType.RECEIVABLE,
        Invoice.status.notin_([InvoiceStatus.CANCELLED, InvoiceStatus.PAID]),
    )
    if business_id:
        recv_q = recv_q.where(Invoice.business_id == business_id)
    total_receivable = (await session.execute(recv_q)).scalar() or Decimal("0.00")

    # Total payable
    pay_q = select(func.coalesce(func.sum(Invoice.total_amount - Invoice.payment_received), 0)).where(
        Invoice.type == InvoiceType.PAYABLE,
        Invoice.status.notin_([InvoiceStatus.CANCELLED, InvoiceStatus.PAID]),
    )
    if business_id:
        pay_q = pay_q.where(Invoice.business_id == business_id)
    total_payable = (await session.execute(pay_q)).scalar() or Decimal("0.00")

    # Overdue
    from datetime import date

    overdue_q = select(func.count(), func.coalesce(func.sum(Invoice.total_amount - Invoice.payment_received), 0)).where(
        Invoice.status.notin_([InvoiceStatus.CANCELLED, InvoiceStatus.PAID]),
        Invoice.due_date < date.today(),
    )
    if business_id:
        overdue_q = overdue_q.where(Invoice.business_id == business_id)
    overdue_result = (await session.execute(overdue_q)).one()

    return InvoiceSummary(
        total_receivable=total_receivable,
        total_payable=total_payable,
        overdue_count=overdue_result[0],
        overdue_amount=overdue_result[1],
    )


@router.get("/{invoice_id}", response_model=InvoiceOut)
async def get_invoice(
    invoice_id: uuid.UUID, session: AsyncSession = Depends(get_session)
):
    invoice = await session.get(Invoice, invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return invoice


@router.patch("/{invoice_id}", response_model=InvoiceOut)
async def update_invoice(
    invoice_id: uuid.UUID,
    data: InvoiceUpdate,
    session: AsyncSession = Depends(get_session),
):
    invoice = await session.get(Invoice, invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    for key, val in data.model_dump(exclude_unset=True).items():
        setattr(invoice, key, val)
    await session.commit()
    await session.refresh(invoice)
    return invoice
