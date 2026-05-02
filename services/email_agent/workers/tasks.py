"""Celery tasks for Email Agent."""

import asyncio
from datetime import datetime, timezone

from email_agent.workers.celery_app import app


def _run_async(coro):
    """Run an async coroutine from sync Celery task."""
    return asyncio.run(coro)


async def _get_session():
    from email_agent.db.engine import async_session
    return async_session()


@app.task(name="email_agent.workers.tasks.sync_gmail")
def sync_gmail():
    """Incremental Gmail sync for all active accounts."""
    return _run_async(_sync_gmail_impl())


async def _sync_gmail_impl():
    from sqlalchemy import select
    from email_agent.models.account import EmailAccount
    from email_agent.services.gmail_client import sync_incremental

    session = await _get_session()
    async with session:
        accounts = (await session.execute(
            select(EmailAccount).where(EmailAccount.is_active == True)  # noqa: E712
        )).scalars().all()

        total_synced = 0
        for account in accounts:
            if not account.gmail_history_id:
                continue  # Need initial full sync first
            synced, new_history_id = await sync_incremental(
                account.id, account.gmail_history_id, session
            )
            account.gmail_history_id = new_history_id
            account.last_sync_at = datetime.now(timezone.utc)
            total_synced += synced

        await session.commit()
        return {"accounts_synced": len(accounts), "messages_synced": total_synced}


@app.task(name="email_agent.workers.tasks.send_scheduled")
def send_scheduled():
    """Send any scheduled emails that are due."""
    return _run_async(_send_scheduled_impl())


async def _send_scheduled_impl():
    from sqlalchemy import select
    from email_agent.models.scheduled import ScheduleStatus, ScheduledEmail
    from email_agent.services.gmail_client import send_email

    session = await _get_session()
    async with session:
        now = datetime.now(timezone.utc)
        q = select(ScheduledEmail).where(
            ScheduledEmail.status == ScheduleStatus.PENDING,
            ScheduledEmail.scheduled_for <= now,
        )
        due = (await session.execute(q)).scalars().all()

        sent_count = 0
        for scheduled in due:
            scheduled.status = ScheduleStatus.SENDING
            await session.commit()

            result = await send_email(
                to=scheduled.to_addresses,
                subject=scheduled.subject,
                body=scheduled.body_text,
                cc=scheduled.cc_addresses,
            )

            if result:
                scheduled.status = ScheduleStatus.SENT
                scheduled.sent_at = datetime.now(timezone.utc)
                sent_count += 1
            else:
                scheduled.status = ScheduleStatus.FAILED
                scheduled.error_message = "Gmail API send failed"

        await session.commit()
        return {"due": len(due), "sent": sent_count}


@app.task(name="email_agent.workers.tasks.renew_gmail_watch")
def renew_gmail_watch():
    """Renew Gmail push notification watch (expires every 7 days)."""
    return _run_async(_renew_watch_impl())


async def _renew_watch_impl():
    from sqlalchemy import select
    from email_agent.config import settings
    from email_agent.models.account import EmailAccount
    from email_agent.services.gmail_client import _get_gmail_service

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

        # Update all active accounts with new history_id
        session = await _get_session()
        async with session:
            accounts = (await session.execute(
                select(EmailAccount).where(EmailAccount.is_active == True)  # noqa: E712
            )).scalars().all()
            for account in accounts:
                if account.email_address == "kunalsingh0036@gmail.com":
                    account.gmail_history_id = history_id
            await session.commit()

        return {"status": "renewed", "history_id": history_id, "expiration": expiration}
    except Exception as e:
        return {"error": str(e)}


@app.task(name="email_agent.workers.tasks.refresh_ngrok_subscription")
def refresh_ngrok_subscription():
    """Check if ngrok URL changed and update Pub/Sub subscription."""
    return _run_async(_refresh_ngrok_impl())


async def _refresh_ngrok_impl():
    import httpx

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get("http://localhost:4040/api/tunnels", timeout=5)
            tunnels = resp.json().get("tunnels", [])

        https_url = None
        for t in tunnels:
            if "https" in t.get("public_url", ""):
                https_url = t["public_url"]
                break

        if not https_url:
            return {"status": "no_tunnel", "action": "none"}

        # Read current stored URL
        from email_agent.config import settings
        current = settings.webhook_base_url

        if current == https_url:
            return {"status": "unchanged", "url": current}

        # URL changed — update settings (runtime only, .env stays)
        settings.webhook_base_url = https_url

        # Update Pub/Sub subscription via gcloud or API
        # For now, log the change — user can update Pub/Sub subscription manually
        # or we can call the Pub/Sub admin API if service account creds are available
        return {
            "status": "url_changed",
            "old_url": current,
            "new_url": https_url,
            "webhook": f"{https_url}/api/v1/webhook/gmail",
            "action": "Update Pub/Sub push subscription endpoint",
        }
    except Exception:
        return {"status": "ngrok_not_running"}


@app.task(name="email_agent.workers.tasks.classify_unprocessed")
def classify_unprocessed():
    """AI-classify emails that haven't been processed yet."""
    return _run_async(_classify_impl())


async def _classify_impl():
    from sqlalchemy import select
    from email_agent.models.email_message import EmailMessage
    from email_agent.services.classifier import classify_email

    session = await _get_session()
    async with session:
        q = select(EmailMessage).where(
            EmailMessage.ai_category.is_(None)
        ).limit(30)  # Batch limit for API cost control
        messages = (await session.execute(q)).scalars().all()

        classified = 0
        for msg in messages:
            result = await classify_email(
                from_address=msg.from_address,
                to_addresses=msg.to_addresses,
                subject=msg.subject,
                body_text=msg.body_text,
                snippet=msg.snippet,
            )
            msg.ai_category = result.category
            msg.ai_priority = result.priority
            msg.ai_summary = result.summary
            msg.ai_action_needed = result.action_needed
            classified += 1

        await session.commit()
        return {"classified": classified, "total_checked": len(messages)}
