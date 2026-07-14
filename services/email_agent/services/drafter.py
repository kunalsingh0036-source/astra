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
from email_agent.models.email_message import EmailDirection, EmailMessage
from email_agent.models.template import EmailTemplate
from email_agent.services.voice import email_voice

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

    # If reply, include the THREAD (not just the one message) so the
    # draft has the actual conversation — what was already said,
    # already agreed, already asked. Replying off a single message in
    # a long thread is how you get a draft that re-asks a question the
    # thread already answered.
    if reply_to_message_id and session:
        original = await session.get(EmailMessage, reply_to_message_id)
        if original:
            thread_block = await _build_thread_context(session, original)
            context_parts.append(thread_block)

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

    # Voice layering, weakest → strongest evidence, all isolated reads:
    #   base guide (hand-written floor) → MINED register profile (how Kunal
    #   actually writes to THIS kind of recipient, from his real sent mail)
    #   → learned edit-corrections. Everything nudges style; the hard rules
    #   (no placeholders/fabrication) stay absolute.
    from email_agent.services.voice_learn import get_email_voice_notes
    from email_agent.services.voice_miner import get_register_profile

    voice = email_voice()
    mined, register = await get_register_profile(to)
    if mined:
        voice += (
            f"\n\nHOW KUNAL ACTUALLY WRITES ({register} register — mined from "
            "his real sent mail; match THIS over the generic guidance above "
            "when they differ):\n" + mined
        )
    learned = await get_email_voice_notes()
    if learned:
        voice += (
            "\n\nLEARNED FROM KUNAL'S ACTUAL EDITS (match these tone/length/"
            "sign-off patterns where they fit naturally):\n" + learned
        )
    if mined or learned:
        voice += (
            "\n\nThe HARD RULES above remain absolute — never relax them, and "
            "never add placeholders, links, recipients, or fabricated facts to "
            "satisfy a pattern."
        )

    prompt = f"""{voice}

---
TASK
Write Kunal's reply.

Recipients: {', '.join(to)}
Intent: {intent}
{"Subject hint: " + subject if subject else "Keep the thread's subject (Re: ...)."}

{context}

Respond in EXACTLY this format and nothing else:
SUBJECT: <the subject line>
---
<the email body, ready for Kunal to send>"""

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
        # A generation failure must NOT surface as a sendable draft.
        # The old behaviour wrote the error string into body_text and
        # marked it READY — so an error message could be presented to
        # Kunal (or sent) as if it were his reply. Mark it DISCARDED
        # with a machine-readable failure flag; the triage/surface
        # layers only ever show READY drafts, so this stays invisible.
        logger.error("Draft generation failed: %s", e)
        draft.body_text = ""
        draft.status = DraftStatus.DISCARDED
        draft.extra_data = {
            **(draft.extra_data or {}),
            "generation_failed": True,
            "error": str(e)[:500],
        }
        if not draft.subject:
            draft.subject = subject or "Draft"

    if session:
        await session.commit()
        await session.refresh(draft)

    return draft


async def _build_thread_context(
    session: AsyncSession, original: EmailMessage
) -> str:
    """Render the recent thread around `original` for the drafter.

    Pulls up to the last 6 messages sharing the same gmail_thread_id,
    oldest-first, each labelled by direction so the model knows who
    said what. Falls back to just the original if the thread can't be
    resolved.
    """
    header = (
        "THREAD YOU ARE REPLYING TO (oldest first; reply to the most "
        "recent inbound message):\n"
    )
    try:
        if not original.gmail_thread_id:
            raise ValueError("no thread id")
        rows = (
            (
                await session.execute(
                    select(EmailMessage)
                    .where(
                        EmailMessage.gmail_thread_id
                        == original.gmail_thread_id
                    )
                    .order_by(EmailMessage.sent_at.asc())
                    .limit(6)
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            rows = [original]
    except Exception:
        rows = [original]

    blocks = []
    for m in rows:
        who = "KUNAL (you)" if m.direction == EmailDirection.OUTBOUND else (
            m.from_address or "them"
        )
        body = (m.body_text or m.snippet or "").strip()[:1500]
        blocks.append(
            f"From: {who}\nSubject: {m.subject or ''}\n{body}"
        )
    return header + "\n\n— — —\n\n".join(blocks)


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

    from email_agent.services.voice_learn import get_email_voice_notes
    from email_agent.services.voice_miner import get_register_profile

    voice = email_voice()
    mined, register = await get_register_profile(list(draft.to_addresses or []))
    if mined:
        voice += (
            f"\n\nHOW KUNAL ACTUALLY WRITES ({register} register — mined from "
            "his real sent mail; match THIS over the generic guidance above "
            "when they differ):\n" + mined
        )
    learned = await get_email_voice_notes()
    if learned:
        voice += (
            "\n\nLEARNED FROM KUNAL'S ACTUAL EDITS (match these tone/length/"
            "sign-off patterns where they fit naturally):\n" + learned
        )
    if mined or learned:
        voice += (
            "\n\nThe HARD RULES above remain absolute — never relax them, and "
            "never add placeholders, links, recipients, or fabricated facts to "
            "satisfy a pattern."
        )

    prompt = f"""{voice}

---
TASK
Revise Kunal's draft below per the instruction. Keep his voice and the
hard rules above (no placeholders, no fabricated facts, stay brief).

Current draft:
Subject: {draft.subject}
Body:
{draft.body_text}

Instruction: {instruction}

Respond in EXACTLY this format and nothing else:
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
        # Surface the failure to the caller instead of silently
        # returning the unchanged draft as if the refine succeeded.
        logger.error("Draft refinement failed: %s", e)
        raise RuntimeError(f"refine failed: {e}") from e

    return draft
