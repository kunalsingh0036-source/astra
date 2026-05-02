"""Business CRUD endpoints."""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from finance.db.engine import get_session
from finance.models.business import Business
from finance.schemas.business import BusinessCreate, BusinessOut, BusinessUpdate

router = APIRouter(prefix="/businesses", tags=["businesses"])


@router.post("/", response_model=BusinessOut, status_code=201)
async def create_business(
    data: BusinessCreate, session: AsyncSession = Depends(get_session)
):
    business = Business(**data.model_dump())
    session.add(business)
    await session.commit()
    await session.refresh(business)
    return business


@router.get("/", response_model=list[BusinessOut])
async def list_businesses(session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(Business).order_by(Business.name))
    return result.scalars().all()


@router.get("/{business_id}", response_model=BusinessOut)
async def get_business(
    business_id: uuid.UUID, session: AsyncSession = Depends(get_session)
):
    business = await session.get(Business, business_id)
    if not business:
        raise HTTPException(status_code=404, detail="Business not found")
    return business


@router.patch("/{business_id}", response_model=BusinessOut)
async def update_business(
    business_id: uuid.UUID,
    data: BusinessUpdate,
    session: AsyncSession = Depends(get_session),
):
    business = await session.get(Business, business_id)
    if not business:
        raise HTTPException(status_code=404, detail="Business not found")
    for key, val in data.model_dump(exclude_unset=True).items():
        setattr(business, key, val)
    await session.commit()
    await session.refresh(business)
    return business
