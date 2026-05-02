"""
Conversation query endpoints — read conversation history.

GET /api/v1/conversations — List conversations with filters
GET /api/v1/conversations/{id}/messages — Full message history
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.db.engine import get_session
from gateway.models.contact import Contact
from gateway.models.conversation import Conversation, ConversationStatus
from gateway.models.message import Message

router = APIRouter(prefix="/api/v1", tags=["Conversations"])


@router.get("/conversations")
async def list_conversations(
    agent_name: str | None = Query(None),
    status: str | None = Query(None),
    phone: str | None = Query(None),
    limit: int = Query(20, le=100),
    offset: int = Query(0),
    session: AsyncSession = Depends(get_session),
):
    """List conversations with optional filters."""
    query = select(Conversation).join(Contact)

    if agent_name:
        query = query.where(Conversation.owning_agent == agent_name)
    if status:
        try:
            query = query.where(Conversation.status == ConversationStatus(status))
        except ValueError:
            pass
    if phone:
        query = query.where(Contact.phone.contains(phone))

    query = query.order_by(Conversation.last_message_at.desc())
    query = query.offset(offset).limit(limit)

    result = await session.execute(query)
    conversations = result.scalars().all()

    return [
        {
            "id": str(c.id),
            "contact_phone": c.contact.phone if c.contact else None,
            "contact_name": c.contact.name if c.contact else None,
            "owning_agent": c.owning_agent,
            "status": c.status.value,
            "message_count": c.message_count,
            "session_open": (
                c.session_expires_at is not None
                and c.session_expires_at > func.now()
            ) if c.session_expires_at else False,
            "last_message_at": c.last_message_at.isoformat() if c.last_message_at else None,
            "created_at": c.created_at.isoformat() if c.created_at else None,
        }
        for c in conversations
    ]


@router.get("/conversations/{conversation_id}/messages")
async def get_conversation_messages(
    conversation_id: str,
    limit: int = Query(50, le=200),
    session: AsyncSession = Depends(get_session),
):
    """Get full message history for a conversation."""
    result = await session.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.asc())
        .limit(limit)
    )
    messages = result.scalars().all()

    if not messages:
        raise HTTPException(404, "Conversation not found or no messages")

    return [
        {
            "id": str(m.id),
            "direction": m.direction.value,
            "type": m.message_type.value,
            "content": m.content,
            "template_name": m.template_name,
            "status": m.status.value,
            "agent_name": m.agent_name,
            "classification": m.classification,
            "created_at": m.created_at.isoformat() if m.created_at else None,
        }
        for m in messages
    ]


@router.get("/stats")
async def get_stats(
    session: AsyncSession = Depends(get_session),
):
    """Gateway statistics."""
    from gateway.models.message import MessageDirection, MessageStatus

    total_contacts = await session.execute(select(func.count(Contact.id)))
    total_convs = await session.execute(select(func.count(Conversation.id)))
    total_msgs = await session.execute(select(func.count(Message.id)))
    outbound = await session.execute(
        select(func.count(Message.id)).where(
            Message.direction == MessageDirection.OUTBOUND
        )
    )
    inbound = await session.execute(
        select(func.count(Message.id)).where(
            Message.direction == MessageDirection.INBOUND
        )
    )
    failed = await session.execute(
        select(func.count(Message.id)).where(
            Message.status == MessageStatus.FAILED
        )
    )

    return {
        "contacts": total_contacts.scalar() or 0,
        "conversations": total_convs.scalar() or 0,
        "messages": {
            "total": total_msgs.scalar() or 0,
            "outbound": outbound.scalar() or 0,
            "inbound": inbound.scalar() or 0,
            "failed": failed.scalar() or 0,
        },
    }
