"""Reconciliation endpoints."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from finance.db.engine import get_session
from finance.models.reconciliation import Reconciliation, ReconciliationType
from finance.schemas.reconciliation import ReconciliationCreate, ReconciliationOut

router = APIRouter(prefix="/reconciliation", tags=["reconciliation"])


@router.post("/", response_model=ReconciliationOut, status_code=201)
async def create_reconciliation(
    data: ReconciliationCreate, session: AsyncSession = Depends(get_session)
):
    recon = Reconciliation(**data.model_dump())
    session.add(recon)
    await session.commit()
    await session.refresh(recon)
    return recon


@router.get("/", response_model=list[ReconciliationOut])
async def list_reconciliations(
    business_id: uuid.UUID | None = None,
    type: ReconciliationType | None = None,
    limit: int = Query(20, le=100),
    session: AsyncSession = Depends(get_session),
):
    q = select(Reconciliation).order_by(Reconciliation.created_at.desc())
    if business_id:
        q = q.where(Reconciliation.business_id == business_id)
    if type:
        q = q.where(Reconciliation.type == type)
    q = q.limit(limit)
    result = await session.execute(q)
    return result.scalars().all()


@router.get("/{recon_id}", response_model=ReconciliationOut)
async def get_reconciliation(
    recon_id: uuid.UUID, session: AsyncSession = Depends(get_session)
):
    recon = await session.get(Reconciliation, recon_id)
    if not recon:
        raise HTTPException(status_code=404, detail="Reconciliation not found")
    return recon
