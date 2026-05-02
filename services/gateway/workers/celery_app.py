"""
Celery configuration for the WhatsApp Gateway.

Uses Astra's Redis instance (DB 1, separate from Astra's DB 0).
"""

from celery import Celery
from celery.schedules import crontab

from gateway.config import settings

celery_app = Celery(
    "whatsapp-gateway",
    broker=settings.redis_url,
    backend=settings.redis_url,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_routes={
        "gateway.workers.dispatch.*": {"queue": "whatsapp_dispatch"},
    },
    beat_schedule={
        "process-outbound-queue": {
            "task": "gateway.workers.dispatch.process_queue",
            "schedule": 30.0,  # Every 30 seconds
        },
    },
)
