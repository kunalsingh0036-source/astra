"""Celery configuration for Email Agent background tasks.

Uses Redis DB 3 (Astra=0, WhatsApp=1, Finance=2, Email=3).
"""

from celery import Celery
from celery.schedules import crontab

from email_agent.config import settings

app = Celery(
    "email_worker",
    broker=settings.redis_url,
    backend=settings.redis_url,
)

app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="Asia/Kolkata",
    enable_utc=True,
    task_default_queue="email",
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    beat_schedule={
        "gmail-sync": {
            "task": "email_agent.workers.tasks.sync_gmail",
            "schedule": 300.0,  # Every 5 minutes
        },
        "send-scheduled-emails": {
            "task": "email_agent.workers.tasks.send_scheduled",
            "schedule": 60.0,  # Every minute
        },
        "classify-unprocessed": {
            "task": "email_agent.workers.tasks.classify_unprocessed",
            "schedule": crontab(minute="*/10"),  # Every 10 minutes
        },
        "renew-gmail-watch": {
            "task": "email_agent.workers.tasks.renew_gmail_watch",
            "schedule": crontab(hour=3, minute=0, day_of_week="*/6"),  # Every 6 days at 3am
        },
        "refresh-ngrok-subscription": {
            "task": "email_agent.workers.tasks.refresh_ngrok_subscription",
            "schedule": 600.0,  # Every 10 minutes — detect URL changes
        },
    },
)

app.autodiscover_tasks(["email_agent.workers"])
