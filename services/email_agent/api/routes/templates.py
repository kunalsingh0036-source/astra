"""Email template endpoints."""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from email_agent.db.engine import get_session
from email_agent.models.template import EmailTemplate
from email_agent.schemas.template import TemplateCreate, TemplateOut, TemplateUpdate

router = APIRouter(prefix="/templates", tags=["templates"])


@router.post("/", response_model=TemplateOut, status_code=201)
async def create_template(
    data: TemplateCreate, session: AsyncSession = Depends(get_session)
):
    template = EmailTemplate(**data.model_dump())
    session.add(template)
    await session.commit()
    await session.refresh(template)
    return template


@router.get("/", response_model=list[TemplateOut])
async def list_templates(
    category: str | None = None,
    session: AsyncSession = Depends(get_session),
):
    q = select(EmailTemplate).order_by(EmailTemplate.name)
    if category:
        q = q.where(EmailTemplate.category == category)
    result = await session.execute(q)
    return result.scalars().all()


@router.get("/{template_id}", response_model=TemplateOut)
async def get_template(
    template_id: uuid.UUID, session: AsyncSession = Depends(get_session)
):
    template = await session.get(EmailTemplate, template_id)
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    return template


@router.patch("/{template_id}", response_model=TemplateOut)
async def update_template(
    template_id: uuid.UUID,
    data: TemplateUpdate,
    session: AsyncSession = Depends(get_session),
):
    template = await session.get(EmailTemplate, template_id)
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    for key, val in data.model_dump(exclude_unset=True).items():
        setattr(template, key, val)
    await session.commit()
    await session.refresh(template)
    return template
