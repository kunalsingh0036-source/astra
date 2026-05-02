"""AI email drafter — generates professional email drafts using Claude Sonnet.

Why Claude Sonnet (not Haiku):
- Email composition requires nuanced tone, context awareness, and creativity
- Sonnet produces significantly better prose than Haiku
- Drafts are low-volume (a few per day), so higher cost is acceptable
- ~$3/MTok input is worth it for professional communication

Drafting modes:
1. Fresh compose — given intent + tone, generate full email
2. Reply — given original thread + intent, generate contextual reply
3. Template-based — fill a template with variables + AI polish
"""

import logging
import uuid

import anthropic
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from email_agent.config import settings
from email_agent.models.draft import Draft, DraftStatus
from email_agent.models.email_message import EmailMessage
from email_agent.models.template import EmailTemplate

logger = logging.getLogger(__name__)


async def generate_draft(
    account_id: uuid.UUID,
    to: list[str],
    intent: str,
    tone: str = "professional",
    subject: str | None = None,
    cc: list[str] | None = None,
    reply_to_message_id: uuid.UUID | None = None,
    template_id: uuid.UUID | None = None,
    session: AsyncSession | None = None,
) -> Draft:
    """Generate an AI email draft.

    Creates a Draft record in GENERATING state, calls Claude Sonnet,
    then updates to READY with the generated content.
    """
    # Create draft record
    draft = Draft(
        account_id=account_id,
        reply_to_message_id=reply_to_message_id,
        to_addresses=to,
        cc_addresses=cc or [],
        subject=subject or "",
        body_text="",
        status=DraftStatus.GENERATING,
        prompt_used=intent,
        tone=tone,
    )

    if session:
        session.add(draft)
        await session.flush()

    # Build context for Claude
    context_parts = []

    # If reply, include original message
    if reply_to_message_id and session:
        original = await session.get(EmailMessage, reply_to_message_id)
        if original:
            context_parts.append(
                f"ORIGINAL EMAIL (replying to):\n"
                f"From: {original.from_address}\n"
                f"Subject: {original.subject}\n"
                f"Body:\n{(original.body_text or original.snippet or '')[:2000]}\n"
            )

    # If template, include it
    if template_id and session:
        template = await session.get(EmailTemplate, template_id)
        if template:
            context_parts.append(
                f"TEMPLATE TO USE:\n"
                f"Subject: {template.subject_template}\n"
                f"Body: {template.body_template}\n"
                f"Variables to fill: {template.variables}\n"
            )

    context = "\n---\n".join(context_parts)

    prompt = f"""Write a professional email draft.

Recipients: {', '.join(to)}
Intent: {intent}
Tone: {tone}
{"Subject hint: " + subject if subject else "Generate an appropriate subject line."}

{context}

Respond in exactly this format:
SUBJECT: <the subject line>
---
<the email body, ready to send — no greeting placeholders, no [Name] — write as Kunal>"""

    try:
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        response = await client.messages.create(
            model=settings.model_sonnet,
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()

        # Parse response
        if "---" in text:
            subject_line, _, body = text.partition("---")
            subject_line = subject_line.replace("SUBJECT:", "").strip()
            body = body.strip()
        else:
            subject_line = subject or "Draft"
            body = text

        draft.subject = subject_line
        draft.body_text = body
        draft.status = DraftStatus.READY

    except Exception as e:
        logger.error("Draft generation failed: %s", e)
        draft.body_text = f"[AI draft generation failed: {e}. Please compose manually.]"
        draft.status = DraftStatus.READY
        if not draft.subject:
            draft.subject = subject or "Draft"

    if session:
        await session.commit()
        await session.refresh(draft)

    return draft


async def refine_draft(
    draft_id: uuid.UUID,
    instruction: str,
    session: AsyncSession,
) -> Draft:
    """Refine an existing draft based on user feedback.

    E.g., "Make it shorter", "Add urgency", "More formal tone"
    """
    draft = await session.get(Draft, draft_id)
    if not draft:
        raise ValueError("Draft not found")

    prompt = f"""Revise this email draft based on the instruction.

Current draft:
Subject: {draft.subject}
Body:
{draft.body_text}

Instruction: {instruction}

Respond in exactly this format:
SUBJECT: <the revised subject line>
---
<the revised email body>"""

    try:
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        response = await client.messages.create(
            model=settings.model_sonnet,
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()

        if "---" in text:
            subject_line, _, body = text.partition("---")
            draft.subject = subject_line.replace("SUBJECT:", "").strip()
            draft.body_text = body.strip()
        else:
            draft.body_text = text

        draft.prompt_used = f"{draft.prompt_used}\n[Refined: {instruction}]"
        await session.commit()
        await session.refresh(draft)

    except Exception as e:
        logger.error("Draft refinement failed: %s", e)

    return draft
