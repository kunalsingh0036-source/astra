"""Gmail API client — handles OAuth2, send, receive, sync.

Why google-api-python-client (not httpx direct):
- Official Google client handles token refresh, pagination, retries, and quota management
- Well-tested OAuth2 flow for service accounts and user consent
- Batch API support for bulk operations

Sync strategy:
- Uses Gmail history API for incremental sync (only new changes since last history_id)
- First sync does a full list+get for recent messages (last 30 days)
- Subsequent syncs are fast: only fetch delta
"""

import base64
import logging
import uuid
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from email_agent.config import settings
from email_agent.models.email_message import EmailDirection, EmailMessage
from email_agent.models.thread import EmailThread

logger = logging.getLogger(__name__)


def _get_gmail_service():
    """Build Gmail API service with OAuth2 credentials.

    Returns None if credentials aren't configured yet.
    """
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build

        SCOPES = [
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.send",
            "https://www.googleapis.com/auth/gmail.modify",
            "https://www.googleapis.com/auth/gmail.labels",
        ]

        creds = None
        token_path = Path(settings.gmail_token_path)
        creds_path = Path(settings.gmail_credentials_path)

        if token_path.exists():
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            elif creds_path.exists():
                flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
                creds = flow.run_local_server(port=0)
            else:
                logger.warning(
                    "Gmail credentials not found. Set up OAuth2 credentials at %s",
                    creds_path,
                )
                return None

            # Save refreshed token
            token_path.parent.mkdir(parents=True, exist_ok=True)
            token_path.write_text(creds.to_json())

        return build("gmail", "v1", credentials=creds)
    except Exception as e:
        logger.error("Failed to initialize Gmail API: %s", e)
        return None


async def send_email(
    to: list[str],
    subject: str,
    body: str,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    reply_to_message_id: str | None = None,
    thread_id: str | None = None,
) -> dict | None:
    """Send an email via Gmail API.

    Returns the sent message metadata or None if Gmail isn't configured.
    """
    service = _get_gmail_service()
    if not service:
        return None

    msg = MIMEMultipart()
    msg["to"] = ", ".join(to)
    msg["subject"] = subject
    if cc:
        msg["cc"] = ", ".join(cc)
    if bcc:
        msg["bcc"] = ", ".join(bcc)
    if reply_to_message_id:
        msg["In-Reply-To"] = reply_to_message_id
        msg["References"] = reply_to_message_id

    msg.attach(MIMEText(body, "plain"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    body_payload: dict[str, Any] = {"raw": raw}
    if thread_id:
        body_payload["threadId"] = thread_id

    try:
        result = service.users().messages().send(
            userId="me", body=body_payload
        ).execute()
        logger.info("Email sent: %s → %s", subject, to)
        return result
    except Exception as e:
        logger.error("Failed to send email: %s", e)
        return None


async def mark_read(gmail_ids: list[str]) -> dict:
    """Mark a batch of Gmail messages read in ONE call (batchModify,
    removing the UNREAD label). Uses the gmail.modify scope (already
    requested at auth). Returns counts so the caller reports honestly."""
    service = _get_gmail_service()
    if not service:
        return {"ok": False, "error": "gmail not configured", "marked": 0}
    if not gmail_ids:
        return {"ok": True, "marked": 0}
    try:
        # batchModify takes up to 1000 ids per call and returns 204/empty.
        service.users().messages().batchModify(
            userId="me",
            body={"ids": gmail_ids, "removeLabelIds": ["UNREAD"]},
        ).execute()
        return {"ok": True, "marked": len(gmail_ids)}
    except Exception as e:  # noqa: BLE001
        logger.warning("[gmail] batch mark_read failed: %s", e)
        return {"ok": False, "marked": 0, "error": str(e)}


async def fetch_messages(
    max_results: int = 50,
    query: str = "",
    label_ids: list[str] | None = None,
) -> list[dict]:
    """Fetch messages from Gmail API.

    Returns parsed message dicts ready for database storage.
    """
    service = _get_gmail_service()
    if not service:
        return []

    try:
        list_params: dict[str, Any] = {"userId": "me", "maxResults": max_results}
        if query:
            list_params["q"] = query
        if label_ids:
            list_params["labelIds"] = label_ids

        results = service.users().messages().list(**list_params).execute()
        messages = results.get("messages", [])

        parsed = []
        for msg_ref in messages:
            msg = service.users().messages().get(
                userId="me", id=msg_ref["id"], format="full"
            ).execute()
            parsed.append(_parse_gmail_message(msg))

        return parsed
    except Exception as e:
        logger.error("Failed to fetch messages: %s", e)
        return []


async def sync_incremental(
    account_id: uuid.UUID,
    history_id: str,
    session: AsyncSession,
) -> tuple[int, str]:
    """Incremental sync using Gmail history API.

    Returns (messages_synced, new_history_id).
    """
    service = _get_gmail_service()
    if not service:
        return (0, history_id)

    try:
        results = service.users().history().list(
            userId="me",
            startHistoryId=history_id,
            historyTypes=["messageAdded"],
        ).execute()

        new_history_id = results.get("historyId", history_id)
        histories = results.get("history", [])

        synced = 0
        for history in histories:
            for added in history.get("messagesAdded", []):
                msg_id = added["message"]["id"]

                # Skip if already in DB
                existing = await session.execute(
                    select(EmailMessage).where(EmailMessage.gmail_message_id == msg_id)
                )
                if existing.scalar_one_or_none():
                    continue

                # Fetch full message
                msg = service.users().messages().get(
                    userId="me", id=msg_id, format="full"
                ).execute()
                parsed = _parse_gmail_message(msg)

                # Resolve or create thread
                thread = await _get_or_create_thread(
                    account_id, parsed["gmail_thread_id"], parsed["subject"],
                    parsed["from_address"], parsed["sent_at"], session
                )

                email_msg = EmailMessage(
                    account_id=account_id,
                    thread_id=thread.id,
                    **parsed,
                )
                session.add(email_msg)
                synced += 1

        await session.commit()
        return (synced, new_history_id)
    except Exception as e:
        logger.error("Incremental sync failed: %s", e)
        return (0, history_id)


async def get_labels() -> list[dict]:
    """Fetch all Gmail labels."""
    service = _get_gmail_service()
    if not service:
        return []

    try:
        results = service.users().labels().list(userId="me").execute()
        return results.get("labels", [])
    except Exception as e:
        logger.error("Failed to fetch labels: %s", e)
        return []


async def modify_labels(
    gmail_id: str,
    *,
    add: list[str] | None = None,
    remove: list[str] | None = None,
) -> dict | None:
    """Add/remove label IDs on a message. Returns the updated message
    dict or None if Gmail isn't configured. Raises on upstream errors.

    Gmail treats INBOX and STARRED as labels — so archiving a message
    is `remove=["INBOX"]` and starring is `add=["STARRED"]`.
    """
    service = _get_gmail_service()
    if not service:
        return None

    body: dict = {}
    if add:
        body["addLabelIds"] = add
    if remove:
        body["removeLabelIds"] = remove

    result = (
        service.users()
        .messages()
        .modify(userId="me", id=gmail_id, body=body)
        .execute()
    )
    return result


def _parse_gmail_message(msg: dict) -> dict:
    """Parse a raw Gmail API message into a flat dict for our model."""
    headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}

    # Parse addresses
    from_addr = headers.get("from", "")
    to_addrs = [a.strip() for a in headers.get("to", "").split(",") if a.strip()]
    cc_addrs = [a.strip() for a in headers.get("cc", "").split(",") if a.strip()]

    # Parse body
    body_text, body_html = _extract_body(msg.get("payload", {}))

    # Parse date
    internal_date = int(msg.get("internalDate", "0"))
    sent_at = datetime.fromtimestamp(internal_date / 1000, tz=timezone.utc)

    # Determine direction
    label_ids = msg.get("labelIds", [])
    direction = EmailDirection.OUTBOUND if "SENT" in label_ids else EmailDirection.INBOUND

    return {
        "gmail_message_id": msg["id"],
        "gmail_thread_id": msg.get("threadId", ""),
        "direction": direction,
        "from_address": from_addr,
        "to_addresses": to_addrs,
        "cc_addresses": cc_addrs,
        "bcc_addresses": [],
        "subject": headers.get("subject", ""),
        "body_text": body_text,
        "body_html": body_html,
        "snippet": msg.get("snippet", ""),
        "sent_at": sent_at,
        "is_read": "UNREAD" not in label_ids,
        "is_starred": "STARRED" in label_ids,
        "is_draft": "DRAFT" in label_ids,
        "has_attachments": _has_attachments(msg.get("payload", {})),
        "gmail_labels": label_ids,
        "in_reply_to": headers.get("in-reply-to"),
    }


def _extract_body(payload: dict) -> tuple[str | None, str | None]:
    """Extract text and HTML body from Gmail message payload."""
    body_text = None
    body_html = None

    if "parts" in payload:
        for part in payload["parts"]:
            mime_type = part.get("mimeType", "")
            data = part.get("body", {}).get("data")
            if data:
                decoded = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
                if mime_type == "text/plain" and not body_text:
                    body_text = decoded
                elif mime_type == "text/html" and not body_html:
                    body_html = decoded
            # Recurse into nested parts
            if "parts" in part:
                nested_text, nested_html = _extract_body(part)
                if nested_text and not body_text:
                    body_text = nested_text
                if nested_html and not body_html:
                    body_html = nested_html
    elif "body" in payload:
        data = payload["body"].get("data")
        if data:
            decoded = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
            mime_type = payload.get("mimeType", "")
            if mime_type == "text/plain":
                body_text = decoded
            elif mime_type == "text/html":
                body_html = decoded

    return body_text, body_html


def _has_attachments(payload: dict) -> bool:
    """Check if a Gmail message has attachments."""
    if "parts" in payload:
        for part in payload["parts"]:
            filename = part.get("filename")
            if filename:
                return True
            if "parts" in part and _has_attachments(part):
                return True
    return False


async def _get_or_create_thread(
    account_id: uuid.UUID,
    gmail_thread_id: str,
    subject: str,
    from_address: str,
    sent_at: datetime,
    session: AsyncSession,
) -> EmailThread:
    """Get existing thread or create a new one."""
    result = await session.execute(
        select(EmailThread).where(EmailThread.gmail_thread_id == gmail_thread_id)
    )
    thread = result.scalar_one_or_none()

    if thread:
        thread.message_count += 1
        thread.last_message_at = sent_at
        if from_address not in thread.participants:
            thread.participants = [*thread.participants, from_address]
    else:
        thread = EmailThread(
            account_id=account_id,
            gmail_thread_id=gmail_thread_id,
            subject=subject,
            participants=[from_address],
            message_count=1,
            first_message_at=sent_at,
            last_message_at=sent_at,
        )
        session.add(thread)

    return thread
