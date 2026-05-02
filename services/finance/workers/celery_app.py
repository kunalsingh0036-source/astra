"""Celery configuration for Finance Agent background tasks.

Uses Redis DB 2 (Astra=0, WhatsApp=1, Finance=2, Email=3).
"""

from celery import Celery
from celery.schedules import crontab

from finance.config import settings

app = Celery(
    "finance_worker",
    broker=settings.redis_url,
    backend=settings.redis_url,
)

app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="Asia/Kolkata",
    enable_utc=True,
    task_default_queue="finance",
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    beat_schedule={
        "daily-cash-flow-snapshot": {
            "task": "finance.workers.tasks.daily_snapshot",
            "schedule": crontab(hour=23, minute=30),  # 11:30 PM IST
        },
        "daily-alert-scan": {
            "task": "finance.workers.tasks.daily_alert_scan",
            "schedule": crontab(hour=8, minute=0),  # 8:00 AM IST
        },
        "overdue-invoice-check": {
            "task": "finance.workers.tasks.check_overdue_invoices",
            "schedule": crontab(hour=9, minute=0),  # 9:00 AM IST
        },
    },
)

app.autodiscover_tasks(["finance.workers"])
