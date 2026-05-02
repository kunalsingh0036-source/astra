"""Email account endpoints."""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from email_agent.db.engine import get_session
from email_agent.models.account import EmailAccount
from email_agent.schemas.account import AccountCreate, AccountOut

router = APIRouter(prefix="/accounts", tags=["accounts"])


@router.post("/", response_model=AccountOut, status_code=201)
async def create_account(
    data: AccountCreate, session: AsyncSession = Depends(get_session)
):
    account = EmailAccount(**data.model_dump())
    session.add(account)
    await session.commit()
    await session.refresh(account)
    return account


@router.get("/", response_model=list[AccountOut])
async def list_accounts(session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(EmailAccount).order_by(EmailAccount.email_address))
    return result.scalars().all()


@router.get("/{account_id}", response_model=AccountOut)
async def get_account(
    account_id: uuid.UUID, session: AsyncSession = Depends(get_session)
):
    account = await session.get(EmailAccount, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    return account
