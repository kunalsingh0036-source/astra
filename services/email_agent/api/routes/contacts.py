"""Contact endpoints."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from email_agent.db.engine import get_session
from email_agent.models.contact import Contact
from email_agent.schemas.contact import ContactCreate, ContactOut, ContactUpdate

router = APIRouter(prefix="/contacts", tags=["contacts"])


@router.post("/", response_model=ContactOut, status_code=201)
async def create_contact(
    data: ContactCreate, session: AsyncSession = Depends(get_session)
):
    contact = Contact(**data.model_dump())
    session.add(contact)
    await session.commit()
    await session.refresh(contact)
    return contact


@router.get("/", response_model=list[ContactOut])
async def list_contacts(
    category: str | None = None,
    limit: int = Query(50, le=500),
    offset: int = 0,
    session: AsyncSession = Depends(get_session),
):
    q = select(Contact).order_by(Contact.last_contacted_at.desc().nulls_last())
    if category:
        q = q.where(Contact.category == category)
    q = q.limit(limit).offset(offset)
    result = await session.execute(q)
    return result.scalars().all()


@router.get("/{contact_id}", response_model=ContactOut)
async def get_contact(
    contact_id: uuid.UUID, session: AsyncSession = Depends(get_session)
):
    contact = await session.get(Contact, contact_id)
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    return contact


@router.patch("/{contact_id}", response_model=ContactOut)
async def update_contact(
    contact_id: uuid.UUID,
    data: ContactUpdate,
    session: AsyncSession = Depends(get_session),
):
    contact = await session.get(Contact, contact_id)
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    for key, val in data.model_dump(exclude_unset=True).items():
        setattr(contact, key, val)
    await session.commit()
    await session.refresh(contact)
    return contact
