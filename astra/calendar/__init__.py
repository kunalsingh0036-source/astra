"""Google Calendar integration — read-only sync of Kunal's schedule.

Writes go through an approval-gated path (same pattern as notes/writeback)
once added. For now, read-only.
"""

from astra.calendar.models import CalendarEvent
from astra.calendar.store import (
    list_events_between,
    list_events_today,
    list_events_tomorrow,
    search_events,
    upsert_event,
)

__all__ = [
    "CalendarEvent",
    "list_events_between",
    "list_events_today",
    "list_events_tomorrow",
    "search_events",
    "upsert_event",
]
