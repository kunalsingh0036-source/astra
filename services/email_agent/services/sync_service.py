"""
Gmail sync — HTTP-triggerable, no Celery required.

History: ingestion was originally a Celery task (workers/tasks.py)
driven by celery-beat. Neither a worker nor beat was ever deployed to
Railway, so the production message store sat at 0 messages while the
service reported healthy — the email pipeline's half of the
"scheduler is half-fiction in the cloud" finding. This module owns
the sync logic; the /api/v1/sync route exposes it; the cloud
scheduler hits that route every few minutes. The Celery task remains
for any future worker deployment but is no longer the only path.

Bootstrap: prod's email_accounts table starts empty (the OAuth dance
happened on the laptop, the row never migrated). When no active
account exists, we create one from the Gmail profile of the
authorized token (GMAIL_TOKEN_JSON env → materialized at startup)
and backfill the last few days so the digest has something to chew
on, then incremental-sync from the profile's historyId.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from email_agent.models.account import EmailAccount
from email_agent.models.email_message import EmailMessage
from email_agent.services.gmail_client import (
    _get_gmail_service,
    _get_or_create_thread,
    fetch_messages,
    sync_incremental,
)

logger = logging.getLogger(__name__)

# Initial backfill window — enough for digests/unanswered to be
# useful immediately without re-ingesting the whole mailbox.
_BOOTSTRAP_QUERY = "newer_than:7d"
_BOOTSTRAP_MAX = 200


async def _store_parsed(
    account_id, parsed: dict, session: AsyncSession
) -> bool:
    """Insert one parsed Gmail message unless it's already stored.

    Returns True if inserted.
    """
    existing = await session.execute(
        select(EmailMessage).where(
            EmailMessage.gmail_message_id == parsed["gmail_message_id"]
        )
    )
    if existing.scalar_one_or_none():
        return False
    thread = await _get_or_create_thread(
        account_id,
        parsed["gmail_thread_id"],
        parsed["subject"],
        parsed["from_address"],
        parsed["sent_at"],
        session,
    )
    session.add(
        EmailMessage(account_id=account_id, thread_id=thread.id, **parsed)
    )
    return True


async def _bootstrap_account(session: AsyncSession) -> EmailAccount | None:
    """Create the account row from the authorized token's profile."""
    service = _get_gmail_service()
    if not service:
        logger.warning(
            "[sync] no Gmail service — token/credentials not materialized?"
        )
        return None
    try:
        profile = service.users().getProfile(userId="me").execute()
    except Exception as e:
        logger.error("[sync] getProfile failed: %s", e)
        return None

    account = EmailAccount(
        email_address=profile["emailAddress"],
        display_name=profile["emailAddress"].split("@")[0],
        is_primary=True,
        is_active=True,
        gmail_history_id=str(profile.get("historyId") or ""),
    )
    session.add(account)
    await session.flush()
    logger.info(
        "[sync] bootstrapped account %s (history_id=%s)",
        account.email_address,
        account.gmail_history_id,
    )

    # Initial backfill so the store isn't empty until new mail arrives.
    parsed_msgs = await fetch_messages(
        max_results=_BOOTSTRAP_MAX, query=_BOOTSTRAP_QUERY
    )
    inserted = 0
    for parsed in parsed_msgs:
        if await _store_parsed(account.id, parsed, session):
            inserted += 1
    logger.info("[sync] backfilled %d message(s)", inserted)
    return account


async def run_sync(session: AsyncSession) -> dict:
    """Sync all active accounts; bootstrap if none exist.

    Returns a summary dict (also the /api/v1/sync response body).
    """
    accounts = (
        (
            await session.execute(
                select(EmailAccount).where(EmailAccount.is_active == True)  # noqa: E712
            )
        )
        .scalars()
        .all()
    )

    bootstrapped = False
    if not accounts:
        account = await _bootstrap_account(session)
        if account is None:
            return {
                "ok": False,
                "error": "no account and bootstrap failed (gmail creds?)",
                "accounts_synced": 0,
                "messages_synced": 0,
            }
        accounts = [account]
        bootstrapped = True

    total = 0
    for account in accounts:
        if not account.gmail_history_id:
            # Account exists but never completed an initial sync —
            # refresh history_id from the profile so incremental can
            # start from "now".
            service = _get_gmail_service()
            if service:
                try:
                    profile = service.users().getProfile(userId="me").execute()
                    account.gmail_history_id = str(profile.get("historyId") or "")
                except Exception as e:
                    logger.error("[sync] profile refresh failed: %s", e)
                    continue
            else:
                continue
        synced, new_history_id = await sync_incremental(
            account.id, account.gmail_history_id, session
        )
        account.gmail_history_id = new_history_id
        account.last_sync_at = datetime.now(timezone.utc)
        total += synced

    await session.commit()
    return {
        "ok": True,
        "bootstrapped": bootstrapped,
        "accounts_synced": len(accounts),
        "messages_synced": total,
    }
