"""Draft endpoints — AI-generated email drafts."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from email_agent.db.engine import get_session
from email_agent.models.draft import Draft, DraftStatus
from email_agent.schemas.draft import DraftCreateRequest, DraftOut, DraftUpdate
from email_agent.services.drafter import generate_draft, refine_draft

router = APIRouter(prefix="/drafts", tags=["drafts"])


@router.post("/generate", response_model=DraftOut)
async def create_draft(
    data: DraftCreateRequest, session: AsyncSession = Depends(get_session)
):
    """Generate an AI email draft."""
    draft = await generate_draft(
        account_id=data.account_id,
        to=data.to,
        intent=data.intent,
        tone=data.tone,
        subject=data.subject,
        cc=data.cc,
        reply_to_message_id=data.reply_to_message_id,
        template_id=data.template_id,
        session=session,
    )
    return draft


class RefineRequest(BaseModel):
    instruction: str


@router.post("/{draft_id}/refine", response_model=DraftOut)
async def refine(
    draft_id: uuid.UUID,
    data: RefineRequest,
    session: AsyncSession = Depends(get_session),
):
    """Refine a draft with feedback."""
    try:
        draft = await refine_draft(draft_id, data.instruction, session)
        return draft
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/", response_model=list[DraftOut])
async def list_drafts(
    status: DraftStatus | None = None,
    limit: int = Query(20, le=100),
    session: AsyncSession = Depends(get_session),
):
    q = select(Draft).order_by(Draft.created_at.desc())
    if status:
        q = q.where(Draft.status == status)
    q = q.limit(limit)
    result = await session.execute(q)
    return result.scalars().all()


@router.get("/{draft_id}", response_model=DraftOut)
async def get_draft(
    draft_id: uuid.UUID, session: AsyncSession = Depends(get_session)
):
    draft = await session.get(Draft, draft_id)
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")
    return draft


@router.patch("/{draft_id}", response_model=DraftOut)
async def update_draft(
    draft_id: uuid.UUID,
    data: DraftUpdate,
    session: AsyncSession = Depends(get_session),
):
    draft = await session.get(Draft, draft_id)
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")
    for key, val in data.model_dump(exclude_unset=True).items():
        setattr(draft, key, val)
    await session.commit()
    await session.refresh(draft)
    return draft


@router.post("/{draft_id}/approve", response_model=DraftOut)
async def approve_draft(
    draft_id: uuid.UUID, session: AsyncSession = Depends(get_session)
):
    draft = await session.get(Draft, draft_id)
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")
    draft.status = DraftStatus.APPROVED
    await session.commit()
    await session.refresh(draft)
    return draft


@router.post("/{draft_id}/discard", response_model=DraftOut)
async def discard_draft(
    draft_id: uuid.UUID, session: AsyncSession = Depends(get_session)
):
    draft = await session.get(Draft, draft_id)
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")
    draft.status = DraftStatus.DISCARDED
    await session.commit()
    await session.refresh(draft)
    return draft
