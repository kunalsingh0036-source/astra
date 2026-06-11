"""
Celery wrappers for outbound dispatch — kept for a future worker
deployment. The logic lives in gateway/services/dispatcher.py; the
PRODUCTION path is the scheduler's wa_dispatch job hitting
POST /api/v1/queue/drain. No Celery worker or beat is deployed to
Railway (and none was ever — these tasks queued messages into a void
for weeks until the drain endpoint shipped 2026-06-12).
"""

import asyncio
import logging

from gateway.services.dispatcher import drain_queue, send_queued_message
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
    """Send a single queued message (Celery wrapper)."""
    return _run_async(send_queued_message(message_id))


@celery_app.task(name="gateway.workers.dispatch.process_queue")
def process_queue() -> dict:
    """Drain the queue (Celery wrapper)."""
    return _run_async(drain_queue())
