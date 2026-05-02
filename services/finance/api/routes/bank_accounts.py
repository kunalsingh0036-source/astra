"""Bank account CRUD endpoints."""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from finance.db.engine import get_session
from finance.models.bank_account import BankAccount
from finance.schemas.bank_account import BankAccountCreate, BankAccountOut, BankAccountUpdate

router = APIRouter(prefix="/bank-accounts", tags=["bank-accounts"])


@router.post("/", response_model=BankAccountOut, status_code=201)
async def create_bank_account(
    data: BankAccountCreate, session: AsyncSession = Depends(get_session)
):
    account = BankAccount(**data.model_dump())
    session.add(account)
    await session.commit()
    await session.refresh(account)
    return account


@router.get("/", response_model=list[BankAccountOut])
async def list_bank_accounts(
    business_id: uuid.UUID | None = None,
    session: AsyncSession = Depends(get_session),
):
    q = select(BankAccount).order_by(BankAccount.bank_name)
    if business_id:
        q = q.where(BankAccount.business_id == business_id)
    result = await session.execute(q)
    return result.scalars().all()


@router.get("/{account_id}", response_model=BankAccountOut)
async def get_bank_account(
    account_id: uuid.UUID, session: AsyncSession = Depends(get_session)
):
    account = await session.get(BankAccount, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Bank account not found")
    return account


@router.patch("/{account_id}", response_model=BankAccountOut)
async def update_bank_account(
    account_id: uuid.UUID,
    data: BankAccountUpdate,
    session: AsyncSession = Depends(get_session),
):
    account = await session.get(BankAccount, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Bank account not found")
    for key, val in data.model_dump(exclude_unset=True).items():
        setattr(account, key, val)
    await session.commit()
    await session.refresh(account)
    return account
