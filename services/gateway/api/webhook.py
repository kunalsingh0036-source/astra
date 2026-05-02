"""
Meta webhook endpoint — receives all inbound WhatsApp messages.

GET  /api/v1/webhook — Meta verification challenge
POST /api/v1/webhook — Inbound messages and status updates

Returns 200 immediately, processes async via background tasks.
"""

import hashlib
import hmac
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.config import settings
from gateway.db.engine import get_session, async_session
from gateway.models.contact import Contact
from gateway.models.conversation import Conversation, ConversationStatus
from gateway.models.message import (
    Message,
    MessageDirection,
    MessageStatus,
    MessageType,
)
from gateway.services.classifier import classify_message
from gateway.services.phone import normalize_phone
from gateway.services.router import InboundRouter
from gateway.services.session import SessionManager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["Webhook"])


# ---------------------------------------------------------------------------
# Verification (Meta handshake)
# ---------------------------------------------------------------------------


@router.get("/webhook")
async def verify_webhook(request: Request):
    """Meta webhook verification challenge."""
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode == "subscribe" and token == settings.whatsapp_verify_token:
        return int(challenge)

    raise HTTPException(403, "Verification failed")


# ---------------------------------------------------------------------------
# Inbound messages
# ---------------------------------------------------------------------------


def _verify_signature(body: bytes, signature: str) -> bool:
    """Verify Meta's HMAC-SHA256 webhook signature."""
    if not settings.whatsapp_app_secret:
        return True  # Skip if not configured (dev mode)

    expected = hmac.new(
        settings.whatsapp_app_secret.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(f"sha256={expected}", signature)


@router.post("/webhook")
async def receive_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
):
    """Receive inbound WhatsApp messages and status updates.

    Returns 200 immediately — Meta requires response within 5 seconds.
    Actual processing happens in background.
    """
    body = await request.body()

    # Verify signature
    signature = request.headers.get("X-Hub-Signature-256", "")
    if settings.whatsapp_app_secret and not _verify_signature(body, signature):
        raise HTTPException(403, "Invalid signature")

    payload = await request.json()

    # Process in background
    background_tasks.add_task(_process_webhook, payload)

    return {"status": "ok"}


async def _process_webhook(payload: dict) -> None:
    """Process a webhook payload in the background.

    Parses Meta's nested format: entry > changes > value > messages/statuses
    """
    try:
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})

                # Process inbound messages
                messages = value.get("messages", [])
                contacts_data = value.get("contacts", [])
                for msg_data in messages:
                    await _handle_inbound_message(msg_data, contacts_data)

                # Process status updates (delivered, read, etc.)
                statuses = value.get("statuses", [])
                for status_data in statuses:
                    await _handle_status_update(status_data)

    except Exception as e:
        logger.error(f"Webhook processing error: {e}", exc_info=True)


async def _handle_inbound_message(
    msg_data: dict, contacts_data: list
) -> None:
    """Handle a single inbound WhatsApp message."""
    async with async_session() as session:
        phone_raw = msg_data.get("from", "")
        wamid = msg_data.get("id", "")
        msg_type = msg_data.get("type", "text")
        timestamp = msg_data.get("timestamp", "")

        # Extract message text
        content = ""
        if msg_type == "text":
            content = msg_data.get("text", {}).get("body", "")
        elif msg_type == "button":
            content = msg_data.get("button", {}).get("text", "")
        elif msg_type == "interactive":
            interactive = msg_data.get("interactive", {})
            if "button_reply" in interactive:
                content = interactive["button_reply"].get("title", "")
            elif "list_reply" in interactive:
                content = interactive["list_reply"].get("title", "")

        # Normalize phone
        try:
            phone = normalize_phone(phone_raw)
        except ValueError:
            phone = phone_raw.strip()

        # Get contact name from Meta's contacts array
        contact_name = None
        for c in contacts_data:
            if c.get("wa_id") == phone_raw:
                profile = c.get("profile", {})
                contact_name = profile.get("name")
                break

        # Deduplicate by external_id
        existing = await session.execute(
            select(Message).where(Message.external_id == wamid)
        )
        if existing.scalar_one_or_none():
            return  # Already processed

        # Find or create contact
        contact_result = await session.execute(
            select(Contact).where(Contact.phone == phone)
        )
        contact = contact_result.scalar_one_or_none()

        if not contact:
            contact = Contact(
                phone=phone,
                name=contact_name,
                source_agent="unknown",
            )
            session.add(contact)
            await session.flush()
        elif contact_name and not contact.name:
            contact.name = contact_name

        # Find or create conversation
        conv_result = await session.execute(
            select(Conversation).where(
                Conversation.contact_id == contact.id,
                Conversation.status.in_([
                    ConversationStatus.ACTIVE,
                    ConversationStatus.PENDING,
                ]),
            ).order_by(Conversation.last_message_at.desc())
        )
        conversation = conv_result.scalar_one_or_none()

        if not conversation:
            conversation = Conversation(
                contact_id=contact.id,
                status=ConversationStatus.PENDING,
            )
            session.add(conversation)
            await session.flush()

        # Open/extend session window
        session_mgr = SessionManager(session)
        await session_mgr.open_session(conversation.id)

        # Route to agent
        inbound_router = InboundRouter(session)
        routing = await inbound_router.route(contact.id, content)

        # Determine agent_name for the message
        agent_name = routing.agent_name or "unassigned"

        # Update conversation ownership if routed
        if routing.agent_name and not conversation.owning_agent:
            conversation.owning_agent = routing.agent_name
            conversation.status = ConversationStatus.ACTIVE

        # Classify the message
        classification_result = await classify_message(content)

        # Create message record
        message = Message(
            conversation_id=conversation.id,
            direction=MessageDirection.INBOUND,
            message_type=MessageType(msg_type) if msg_type in MessageType.__members__.values() else MessageType.TEXT,
            content=content,
            status=MessageStatus.DELIVERED,
            external_id=wamid,
            agent_name=agent_name,
            classification=classification_result.label,
            classification_confidence=classification_result.confidence,
            extra_data={
                "raw_type": msg_type,
                "timestamp": timestamp,
                "routing": {
                    "agent": routing.agent_name,
                    "confidence": routing.confidence,
                    "action": routing.action,
                    "reason": routing.reason,
                },
                "classification_method": classification_result.method,
            },
        )
        session.add(message)

        # Update conversation counters
        conversation.last_message_at = datetime.now(timezone.utc)
        conversation.message_count += 1

        await session.commit()

        # Forward to agent if routed
        if routing.action == "forward" and routing.agent_name:
            await inbound_router.forward_to_agent(
                routing.agent_name,
                {
                    "type": "whatsapp_inbound",
                    "message_id": str(message.id),
                    "conversation_id": str(conversation.id),
                    "phone": phone,
                    "contact_name": contact_name,
                    "content": content,
                    "classification": classification_result.label,
                    "timestamp": timestamp,
                },
            )

        logger.info(
            f"Inbound from {phone}: '{content[:50]}' → "
            f"routed to {routing.agent_name} ({routing.action})"
        )


async def _handle_status_update(status_data: dict) -> None:
    """Handle a message status update (sent, delivered, read, failed)."""
    async with async_session() as session:
        wamid = status_data.get("id", "")
        status = status_data.get("status", "")

        if not wamid:
            return

        result = await session.execute(
            select(Message).where(Message.external_id == wamid)
        )
        message = result.scalar_one_or_none()

        if not message:
            return

        status_map = {
            "sent": MessageStatus.SENT,
            "delivered": MessageStatus.DELIVERED,
            "read": MessageStatus.READ,
            "failed": MessageStatus.FAILED,
        }

        new_status = status_map.get(status)
        if new_status:
            message.status = new_status
            if status == "failed":
                errors = status_data.get("errors", [])
                if errors:
                    message.extra_data = {
                        **(message.extra_data or {}),
                        "error": errors[0],
                    }
            await session.commit()

        logger.debug(f"Status update: {wamid} → {status}")
