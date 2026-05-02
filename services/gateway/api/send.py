"""
Send endpoint — the primary interface for agents to send WhatsApp messages.

POST /api/v1/send
- Validates agent, contact, cooldown, and session window
- Queues the message for async dispatch
- Returns message_id and conversation_id
"""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.db.engine import get_session
from gateway.models.agent_registration import AgentRegistration
from gateway.models.contact import Contact
from gateway.models.conversation import Conversation, ConversationStatus
from gateway.models.message import Message, MessageDirection, MessageStatus, MessageType
from gateway.services.dedup import CooldownService
from gateway.services.phone import get_country_code, normalize_phone
from gateway.services.session import SessionManager

router = APIRouter(prefix="/api/v1", tags=["Send"])


# ---------------------------------------------------------------------------
# Request/Response models
# ---------------------------------------------------------------------------


class SendRequest(BaseModel):
    agent_name: str = Field(description="Sending agent's registered name")
    phone: str = Field(description="Recipient phone number (any format)")
    message_type: str = Field(
        default="text",
        description="text, template, image, document, video",
    )
    content: str | None = Field(
        default=None, description="Message body (for text messages)"
    )
    template_name: str | None = Field(
        default=None, description="Template name (for template messages)"
    )
    template_language: str = Field(default="en")
    template_components: list | None = Field(
        default=None, description="Template variable components"
    )
    media_url: str | None = Field(
        default=None, description="Media URL (for media messages)"
    )
    media_caption: str | None = Field(default=None)
    contact_name: str | None = Field(
        default=None, description="Contact name (used if creating new contact)"
    )


class SendResponse(BaseModel):
    message_id: str
    conversation_id: str
    status: str
    session_open: bool


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post("/send", response_model=SendResponse)
async def send_message(
    req: SendRequest,
    session: AsyncSession = Depends(get_session),
):
    """Queue a WhatsApp message for sending.

    Validates:
    1. Agent is registered and active
    2. Phone number is valid
    3. Cooldown allows sending
    4. Session window allows message type

    Returns the queued message ID.
    """
    # 1. Validate agent
    agent_result = await session.execute(
        select(AgentRegistration).where(
            AgentRegistration.name == req.agent_name,
            AgentRegistration.is_active == True,
        )
    )
    agent = agent_result.scalar_one_or_none()
    if not agent:
        raise HTTPException(404, f"Agent '{req.agent_name}' not registered or inactive")

    # 2. Normalize phone
    try:
        phone = normalize_phone(req.phone)
    except ValueError as e:
        raise HTTPException(400, str(e))

    country = get_country_code(req.phone)

    # 3. Find or create contact
    contact_result = await session.execute(
        select(Contact).where(Contact.phone == phone)
    )
    contact = contact_result.scalar_one_or_none()

    if not contact:
        contact = Contact(
            phone=phone,
            name=req.contact_name,
            country_code=country,
            source_agent=req.agent_name,
        )
        session.add(contact)
        await session.flush()

    # 4. Find or create conversation
    conv_result = await session.execute(
        select(Conversation).where(
            Conversation.contact_id == contact.id,
            Conversation.status == ConversationStatus.ACTIVE,
        )
    )
    conversation = conv_result.scalar_one_or_none()

    if not conversation:
        conversation = Conversation(
            contact_id=contact.id,
            owning_agent=req.agent_name,
            status=ConversationStatus.ACTIVE,
        )
        session.add(conversation)
        await session.flush()

    # 5. Check cooldown
    cooldown_svc = CooldownService(session)
    can_send, reason = await cooldown_svc.can_send(contact.id, req.agent_name)
    if not can_send:
        raise HTTPException(429, f"Cooldown active: {reason}")

    # 6. Check session window
    msg_type = MessageType(req.message_type)
    session_mgr = SessionManager(session)
    allowed, session_reason = await session_mgr.validate_outbound(
        conversation.id, msg_type
    )
    if not allowed:
        raise HTTPException(
            409,
            f"Session window: {session_reason}. Use template_name for outbound.",
        )

    # 7. Create message
    extra_data = {}
    if req.template_components:
        extra_data["template_components"] = req.template_components
    if req.media_url:
        extra_data["media_url"] = req.media_url
        extra_data["media_caption"] = req.media_caption

    message = Message(
        conversation_id=conversation.id,
        direction=MessageDirection.OUTBOUND,
        message_type=msg_type,
        content=req.content,
        template_name=req.template_name,
        status=MessageStatus.QUEUED,
        agent_name=req.agent_name,
        extra_data=extra_data,
    )
    session.add(message)

    # Update conversation
    conversation.last_message_at = datetime.now(timezone.utc)
    conversation.message_count += 1

    await session.commit()

    session_open = await session_mgr.is_session_open(conversation.id)

    return SendResponse(
        message_id=str(message.id),
        conversation_id=str(conversation.id),
        status="queued",
        session_open=session_open,
    )
