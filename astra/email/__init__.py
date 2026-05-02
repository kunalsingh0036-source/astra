"""Email reading — Astra's lens on Kunal's Gmail.

email-agent (port 8005) owns Gmail auth, ingestion, and storage. This
module is Astra's *reader* — it pulls structured signal out of the
agent for briefings, research context, and the person-CRM.

Three layers:

  * `client.py`       — thin HTTP client over email-agent
  * `signals.py`      — digests + unanswered threads + sender-frequency
  * `personize.py`    — extract / maintain a lightweight person-CRM
                        from sender addresses seen over time

Nothing here writes to Gmail. Replies & sends continue to flow through
email-agent's own /messages/send endpoint with Kunal's explicit approval.
"""

from astra.email.classify import classify_sweep
from astra.email.client import (
    get_summary,
    list_messages,
    get_message,
    search_messages,
)
from astra.email.signals import (
    daily_digest,
    unanswered_incoming,
    top_senders_window,
)

__all__ = [
    "classify_sweep",
    "get_summary",
    "list_messages",
    "get_message",
    "search_messages",
    "daily_digest",
    "unanswered_incoming",
    "top_senders_window",
]
