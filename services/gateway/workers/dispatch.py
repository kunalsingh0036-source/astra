"""
Outbound message dispatcher.

Picks up queued messages and sends them via the Meta API.
Runs session window + cooldown checks at send time (not just queue time).
"""

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from gateway.db.engine import async_session
from gateway.models.contact import Contact
from gateway.models.conversation import Conversation
from gateway.models.message import Message, MessageStatus, MessageType
from gateway.services.dedup import CooldownService
from gateway.services.meta_api import MetaAPIClient
from gateway.services.session import SessionManager
from gateway.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


def _run_async(coro):
    """Run an async function from sync Celery task context."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@celery_app.task(name="gateway.workers.dispatch.send_message")
def send_message(message_id: str) -> dict:
    """Send a single queued message.

    Validates session window and cooldown at send time (not just queue time)
    to handle race conditions.
    """
    return _run_async(_send_message(message_id))


async def _send_message(message_id: str) -> dict:
    """Async implementation of message sending."""
    meta_client = MetaAPIClient()

    try:
        async with async_session() as session:
            # Load message with conversation and contact
            msg = await session.get(Message, message_id)
            if not msg:
                return {"error": f"Message {message_id} not found"}

            if msg.status != MessageStatus.QUEUED:
                return {"error": f"Message not queued (status={msg.status.value})"}

            conv = await session.get(Conversation, msg.conversation_id)
            if not conv:
                msg.status = MessageStatus.FAILED
                msg.extra_data = {**(msg.extra_data or {}), "error": "Conversation not found"}
                await session.commit()
                return {"error": "Conversation not found"}

            contact = await session.get(Contact, conv.contact_id)
            if not contact:
                msg.status = MessageStatus.FAILED
                await session.commit()
                return {"error": "Contact not found"}

            # Re-check session window at send time
            session_mgr = SessionManager(session)
            allowed, reason = await session_mgr.validate_outbound(
                conv.id, msg.message_type
            )
            if not allowed:
                msg.status = MessageStatus.REJECTED
                msg.extra_data = {**(msg.extra_data or {}), "error": reason}
                await session.commit()
                return {"error": reason}

            # Re-check cooldown at send time
            cooldown_svc = CooldownService(session)
            can_send, cd_reason = await cooldown_svc.can_send(
                contact.id, msg.agent_name
            )
            if not can_send:
                msg.status = MessageStatus.REJECTED
                msg.extra_data = {**(msg.extra_data or {}), "error": cd_reason}
                await session.commit()
                return {"error": cd_reason}

            # Send via Meta API
            phone = contact.phone

            if msg.message_type == MessageType.TEMPLATE:
                result = await meta_client.send_template(
                    phone=phone,
                    template_name=msg.template_name or "",
                    language_code=(msg.extra_data or {}).get("template_language", "en"),
                    components=(msg.extra_data or {}).get("template_components"),
                )
            elif msg.message_type in (
                MessageType.IMAGE, MessageType.DOCUMENT, MessageType.VIDEO
            ):
                result = await meta_client.send_media(
                    phone=phone,
                    media_type=msg.message_type.value,
                    media_url=(msg.extra_data or {}).get("media_url", ""),
                    caption=(msg.extra_data or {}).get("media_caption", ""),
                )
            else:
                result = await meta_client.send_text(
                    phone=phone,
                    body=msg.content or "",
                )

            # Update message status
            if result.success:
                msg.status = MessageStatus.SENT
                msg.external_id = result.message_id

                # Record cooldown
                await cooldown_svc.record_send(contact.id, msg.agent_name)

                logger.info(
                    f"Sent {msg.message_type.value} to {phone} "
                    f"(agent={msg.agent_name}, wamid={result.message_id})"
                )
            else:
                msg.status = MessageStatus.FAILED
                msg.extra_data = {
                    **(msg.extra_data or {}),
                    "error": result.error,
                    "status_code": result.status_code,
                }
                logger.error(
                    f"Failed to send to {phone}: {result.error}"
                )

            await session.commit()
            return {
                "success": result.success,
                "message_id": str(msg.id),
                "external_id": result.message_id,
            }

    finally:
        await meta_client.close()


@celery_app.task(name="gateway.workers.dispatch.process_queue")
def process_queue() -> dict:
    """Process all queued messages. Runs every 30 seconds via Celery Beat."""
    return _run_async(_process_queue())


async def _process_queue() -> dict:
    """Pick up queued messages and dispatch them."""
    async with async_session() as session:
        result = await session.execute(
            select(Message.id)
            .where(Message.status == MessageStatus.QUEUED)
            .order_by(Message.created_at)
            .limit(50)
        )
        message_ids = [str(row[0]) for row in result.all()]

    # Dispatch each as a separate Celery task
    for mid in message_ids:
        send_message.delay(mid)

    if message_ids:
        logger.info(f"Dispatched {len(message_ids)} queued messages")

    return {"dispatched": len(message_ids)}
