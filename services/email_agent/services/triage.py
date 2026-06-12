"""
Silent inbox triage — stage draft replies for action-needed mail.

The operating-mode contract is "silent triage before 13:00 IST":
by the time Kunal looks up from the morning, everything that needs
a reply should already have one WAITING, not a todo to write one.
This module finds recent inbound messages classified action-needed
that don't have a draft yet and generates one each via the existing
drafter (Claude). Drafts land in DraftStatus.READY — nothing is
sent without Kunal touching it.

Called by POST /api/v1/ai/triage (mesh-auth) from the scheduler's
inbox_triage job. Caps per run keep cost bounded; the job runs
often enough that the cap never backs up in practice.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from email_agent.models.account import EmailAccount
from email_agent.models.draft import Draft
from email_agent.models.email_message import EmailDirection, EmailMessage
from email_agent.services.drafter import generate_draft

logger = logging.getLogger(__name__)

_WINDOW_HOURS = 36
_MAX_DRAFTS_PER_RUN = 5


async def triage_and_draft(session: AsyncSession) -> dict:
    """Generate reply drafts for recent action-needed inbound mail."""
    account = (
        (
            await session.execute(
                select(EmailAccount).where(EmailAccount.is_active == True)  # noqa: E712
            )
        )
        .scalars()
        .first()
    )
    if account is None:
        return {"ok": False, "error": "no active account", "drafted": 0}

    since = datetime.now(timezone.utc) - timedelta(hours=_WINDOW_HOURS)

    # Inbound, action-needed, recent.
    candidates = (
        (
            await session.execute(
                select(EmailMessage)
                .where(
                    EmailMessage.ai_action_needed == True,  # noqa: E712
                    EmailMessage.direction == EmailDirection.INBOUND,
                    EmailMessage.sent_at >= since,
                )
                .order_by(EmailMessage.sent_at.desc())
                .limit(25)
            )
        )
        .scalars()
        .all()
    )

    # Skip anything that already has a draft.
    drafted_ids = {
        row[0]
        for row in (
            await session.execute(
                select(Draft.reply_to_message_id).where(
                    Draft.reply_to_message_id.is_not(None)
                )
            )
        ).all()
    }

    drafted = 0
    skipped = 0
    for msg in candidates:
        if drafted >= _MAX_DRAFTS_PER_RUN:
            break
        if msg.id in drafted_ids:
            skipped += 1
            continue
        try:
            await generate_draft(
                account_id=account.id,
                to=[msg.from_address],
                intent=(
                    "Draft a reply to this email on Kunal's behalf. Be "
                    "direct and brief; commit only to things the email "
                    "itself makes safe to commit to; if a decision or "
                    "data point is needed from Kunal, structure the "
                    "reply so it's easy for him to fill in."
                ),
                tone="professional",
                subject=f"Re: {msg.subject or ''}".strip(),
                reply_to_message_id=msg.id,
                session=session,
            )
            drafted += 1
        except Exception:
            logger.exception("[triage] draft failed for message %s", msg.id)

    await session.commit()
    result = {
        "ok": True,
        "candidates": len(candidates),
        "drafted": drafted,
        "already_drafted": skipped,
    }
    logger.info("[triage] %s", result)
    return result
