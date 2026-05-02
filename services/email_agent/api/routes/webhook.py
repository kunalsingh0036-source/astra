"""Gmail Pub/Sub webhook — receives push notifications for new emails.

Flow:
1. Gmail detects a mailbox change (new email, label change, etc.)
2. Google publishes a message to our Pub/Sub topic
3. Pub/Sub pushes the message to this webhook endpoint
4. We decode the notification and trigger an incremental sync
5. Return 200 to acknowledge (Pub/Sub retries on non-2xx)

The notification payload contains:
- emailAddress: which Gmail account changed
- historyId: the history ID to sync from
"""

import base64
import json
import logging

from fastapi import APIRouter, Request, Response
from sqlalchemy import select

from email_agent.db.engine import async_session
from email_agent.models.account import EmailAccount
from email_agent.services.gmail_client import sync_incremental

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhook", tags=["webhook"])

# In-memory push diagnostics. Not authoritative (reset on restart)
# but invaluable when debugging why Gmail pushes are or aren't
# arriving — exposed via GET /webhook/gmail/diag.
_push_counter: dict[str, object] = {
    "total_received": 0,
    "successful_syncs": 0,
    "last_received_at": None,
    "last_history_id": None,
    "last_email_address": None,
    "last_error": None,
}


@router.post("/gmail")
async def gmail_push(request: Request):
    """Receive Gmail Pub/Sub push notification and trigger sync."""
    from datetime import datetime, timezone

    _push_counter["total_received"] = int(_push_counter["total_received"]) + 1  # type: ignore[arg-type]
    _push_counter["last_received_at"] = datetime.now(timezone.utc).isoformat()

    try:
        body = await request.json()
    except Exception:
        _push_counter["last_error"] = "invalid json"
        return Response(status_code=400)

    # Pub/Sub wraps the data in message.data (base64-encoded)
    message = body.get("message", {})
    data_b64 = message.get("data", "")

    if not data_b64:
        logger.warning("Gmail push: empty data")
        return Response(status_code=200)  # Ack to stop retries

    try:
        decoded = json.loads(base64.b64decode(data_b64))
    except Exception:
        logger.warning("Gmail push: failed to decode data")
        return Response(status_code=200)

    email_address = decoded.get("emailAddress", "")
    history_id = str(decoded.get("historyId", ""))

    _push_counter["last_email_address"] = email_address
    _push_counter["last_history_id"] = history_id
    _push_counter["last_error"] = None

    if not email_address or not history_id:
        _push_counter["last_error"] = "missing emailAddress or historyId"
        logger.warning("Gmail push: missing emailAddress or historyId")
        return Response(status_code=200)

    logger.info("Gmail push: %s historyId=%s", email_address, history_id)

    # Find matching account and sync
    async with async_session() as session:
        result = await session.execute(
            select(EmailAccount).where(
                EmailAccount.email_address == email_address,
                EmailAccount.is_active == True,  # noqa: E712
            )
        )
        account = result.scalar_one_or_none()

        if not account:
            logger.warning("Gmail push: no account for %s", email_address)
            return Response(status_code=200)

        # Use the account's last known history_id if it's newer
        sync_from = account.gmail_history_id or history_id

        synced, new_history_id = await sync_incremental(
            account_id=account.id,
            history_id=sync_from,
            session=session,
        )

        # Update account's history_id and last_sync
        account.gmail_history_id = new_history_id
        from datetime import datetime, timezone
        account.last_sync_at = datetime.now(timezone.utc)
        await session.commit()

        logger.info(
            "Gmail push sync: %s synced %d messages, historyId=%s",
            email_address, synced, new_history_id,
        )
        _push_counter["successful_syncs"] = int(_push_counter["successful_syncs"]) + 1  # type: ignore[arg-type]

    return Response(status_code=200)


@router.get("/gmail/diag")
async def gmail_push_diag():
    """Diagnostic view of the Gmail push pipeline.

    Shows push counter state since the email-agent last started, the
    current watch expiration, and the configured Pub/Sub topic. Use
    this to verify Pub/Sub is actually delivering (total_received > 0)
    vs. silently mis-configured in GCP Console.
    """
    from email_agent.config import settings

    return {
        "counters": _push_counter,
        "configured_topic": settings.pubsub_topic,
        "webhook_url_expected": "https://email.thearrogantclub.com/api/v1/webhook/gmail",
    }


@router.post("/gmail/watch")
async def register_watch():
    """Register Gmail push notifications via users().watch().

    Must be called:
    - Once on initial setup
    - Every 7 days to renew (watch expires)
    """
    from email_agent.services.gmail_client import _get_gmail_service
    from email_agent.config import settings

    service = _get_gmail_service()
    if not service:
        return {"error": "Gmail not configured"}

    try:
        result = service.users().watch(
            userId="me",
            body={
                "topicName": settings.pubsub_topic,
                "labelIds": ["INBOX"],
            },
        ).execute()

        history_id = result.get("historyId")
        expiration = result.get("expiration")

        logger.info("Gmail watch registered: historyId=%s, expires=%s", history_id, expiration)

        # Store the history_id on the account
        async with async_session() as session:
            accounts = await session.execute(
                select(EmailAccount).where(EmailAccount.is_active == True)  # noqa: E712
            )
            for account in accounts.scalars():
                account.gmail_history_id = history_id
            await session.commit()

        return {
            "status": "watching",
            "history_id": history_id,
            "expiration": expiration,
            "topic": settings.pubsub_topic,
        }
    except Exception as e:
        logger.error("Gmail watch failed: %s", e)
        return {"error": str(e)}
