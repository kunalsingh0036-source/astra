"""Draft endpoints — AI-generated email drafts."""

import difflib
import logging
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from email_agent.db.engine import get_session
from email_agent.models.draft import Draft, DraftStatus
from email_agent.models.email_message import EmailMessage
from email_agent.schemas.draft import DraftCreateRequest, DraftOut, DraftUpdate
from email_agent.services.drafter import generate_draft, refine_draft
from email_agent.services.gmail_client import mark_read as gmail_mark_read
from email_agent.services.gmail_client import send_email

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/drafts", tags=["drafts"])


def _change_fraction(before: str, after: str) -> float:
    """0.0 = identical, 1.0 = completely rewritten. Used to measure how
    much Kunal edited a draft before sending — the cheapest available
    signal for draft QUALITY (low edits = the draft was good)."""
    before = (before or "").strip()
    after = (after or "").strip()
    if not before and not after:
        return 0.0
    sim = difflib.SequenceMatcher(None, before, after).ratio()
    return round(1.0 - sim, 3)


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
    except RuntimeError as e:
        # Generation provider failed — report it instead of returning
        # the unchanged draft as if the refine had taken effect.
        raise HTTPException(status_code=502, detail=str(e))


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


@router.get("/metrics")
async def draft_metrics(
    days: int = Query(7, ge=1, le=90),
    session: AsyncSession = Depends(get_session),
):
    """The Friday number for the inbox beachhead.

    Cohorts drafts by creation date over the window and reports how the
    funnel resolved: generated → sent (as-is / edited) / discarded /
    still-pending. draft_sent_rate is sent ÷ decided (a discarded draft
    is a 'no', a pending one hasn't been decided yet). Generation
    failures are excluded — they were never real drafts.

    Defined BEFORE the /{draft_id} route on purpose: otherwise FastAPI
    matches 'metrics' against the UUID path param and 422s.
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (
        (
            await session.execute(
                select(Draft).where(Draft.created_at >= since)
            )
        )
        .scalars()
        .all()
    )
    drafts = [
        d for d in rows if not (d.extra_data or {}).get("generation_failed")
    ]

    sent = [d for d in drafts if d.status == DraftStatus.SENT]
    discarded = [d for d in drafts if d.status == DraftStatus.DISCARDED]
    pending = [
        d
        for d in drafts
        if d.status
        in (DraftStatus.READY, DraftStatus.APPROVED, DraftStatus.GENERATING)
    ]
    sent_edited = sum(1 for d in sent if (d.extra_data or {}).get("was_edited"))
    sent_as_is = len(sent) - sent_edited
    decided = len(sent) + len(discarded)
    rate = round(len(sent) / decided, 3) if decided else None

    # ~6 minutes is a conservative estimate of what it takes to write a
    # considered reply from a blank box. Edited sends still saved the
    # blank-page time, so all sends count.
    minutes_saved = len(sent) * 6

    return {
        "window_days": days,
        "generated": len(drafts),
        "sent": len(sent),
        "sent_as_is": sent_as_is,
        "sent_edited": sent_edited,
        "discarded": len(discarded),
        "pending": len(pending),
        "draft_sent_rate": rate,
        "est_minutes_saved": minutes_saved,
    }


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


class SendDraftRequest(BaseModel):
    # The body/subject Kunal actually approved. If omitted, the stored
    # draft is sent verbatim. When present we diff against the original
    # to measure how much he changed it.
    body_override: str | None = None
    subject_override: str | None = None


@router.post("/{draft_id}/send")
async def send_draft(
    draft_id: uuid.UUID,
    data: SendDraftRequest | None = None,
    session: AsyncSession = Depends(get_session),
):
    """Send a draft through Gmail and CLOSE the loop.

    This is the one place a draft becomes a real sent email. It flips
    the draft to SENT, stamps the send metadata (the source of the
    draft-sent-rate metric), records how much Kunal edited it, and
    marks the original inbound message read so the thread leaves the
    'needs a reply' set. Nothing else in the system sends on Kunal's
    behalf — the loop is human-approved end to end.
    """
    data = data or SendDraftRequest()
    draft = await session.get(Draft, draft_id)
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")
    if draft.status == DraftStatus.SENT:
        raise HTTPException(status_code=409, detail="Draft already sent")
    if draft.status not in (DraftStatus.READY, DraftStatus.APPROVED):
        raise HTTPException(
            status_code=400,
            detail=f"Draft not sendable (status={draft.status.value})",
        )

    original_body = (draft.body_text or "").strip()
    final_body = (data.body_override or draft.body_text or "").strip()
    final_subject = (data.subject_override or draft.subject or "").strip()
    if not final_body:
        raise HTTPException(status_code=400, detail="Refusing to send an empty body")
    if not draft.to_addresses:
        raise HTTPException(status_code=400, detail="Draft has no recipient")

    # Resolve the thread so Gmail keeps the reply in-thread, and grab
    # the original message to mark it read afterwards.
    original_msg: EmailMessage | None = None
    thread_id: str | None = None
    if draft.reply_to_message_id:
        original_msg = await session.get(EmailMessage, draft.reply_to_message_id)
        if original_msg:
            thread_id = original_msg.gmail_thread_id

    result = await send_email(
        to=list(draft.to_addresses),
        subject=final_subject,
        body=final_body,
        cc=list(draft.cc_addresses) if draft.cc_addresses else None,
        thread_id=thread_id,
    )
    if result is None:
        raise HTTPException(
            status_code=503,
            detail="Gmail API not configured. Set up OAuth2 credentials first.",
        )

    was_edited = final_body != original_body
    draft.body_text = final_body
    draft.subject = final_subject
    draft.status = DraftStatus.SENT
    draft.gmail_draft_id = draft.gmail_draft_id  # unchanged; reserved
    draft.extra_data = {
        **(draft.extra_data or {}),
        "sent_at": datetime.now(timezone.utc).isoformat(),
        "sent_gmail_id": result.get("id"),
        "sent_thread_id": result.get("threadId"),
        "was_edited": was_edited,
        "change_fraction": _change_fraction(original_body, final_body),
        # keep the AI's original only when edited, for later quality review
        "ai_original_body": original_body if was_edited else None,
    }
    await session.commit()

    # Close the loop: the inbound message that prompted this reply is
    # now handled. Best-effort — a mark-read failure must not fail the
    # send (the email already went out).
    if original_msg and original_msg.gmail_message_id:
        try:
            res = await gmail_mark_read([original_msg.gmail_message_id])
            if res.get("ok"):
                original_msg.is_read = True
                if original_msg.gmail_labels:
                    original_msg.gmail_labels = [
                        l for l in original_msg.gmail_labels if l != "UNREAD"
                    ]
                await session.commit()
        except Exception as e:
            logger.warning("[drafts] post-send mark-read failed: %s", e)

    return {
        "status": "sent",
        "draft_id": str(draft.id),
        "gmail_id": result.get("id"),
        "thread_id": result.get("threadId"),
        "was_edited": was_edited,
    }
