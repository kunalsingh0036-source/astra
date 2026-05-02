"""Email agent models — re-export all for Alembic and convenience."""

from email_agent.models.account import EmailAccount
from email_agent.models.email_message import EmailMessage, EmailDirection
from email_agent.models.thread import EmailThread, ThreadPriority
from email_agent.models.contact import Contact
from email_agent.models.template import EmailTemplate
from email_agent.models.label import Label
from email_agent.models.draft import Draft, DraftStatus
from email_agent.models.scheduled import ScheduledEmail, ScheduleStatus

__all__ = [
    "EmailAccount",
    "EmailMessage", "EmailDirection",
    "EmailThread", "ThreadPriority",
    "Contact",
    "EmailTemplate",
    "Label",
    "Draft", "DraftStatus",
    "ScheduledEmail", "ScheduleStatus",
]
