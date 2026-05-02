"""
Google Calendar event model — one row per event, synced from the
primary calendar every 10 minutes. Mirrors Google faithfully; the
briefing interprets.

Design choice: we store `start_at` / `end_at` as tz-aware UTC timestamps
plus a `tz` string for the event's native timezone, so rendering in IST
is trivial without losing Google's original tz metadata. All-day events
are stored with `start_at` at 00:00 UTC-equivalent and `is_all_day=True`.
"""

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from astra.db.engine import Base


class CalendarEvent(Base):
    __tablename__ = "calendar_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Google's stable event id. Unique per calendar, used as upsert key.
    google_id: Mapped[str] = mapped_column(
        String(256), unique=True, index=True, nullable=False
    )
    # Which calendar this came from — "primary" for Kunal's main cal,
    # or the calendarId of a shared cal.
    calendar_id: Mapped[str] = mapped_column(
        String(256), default="primary", nullable=False, index=True
    )

    summary: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    location: Mapped[str] = mapped_column(String(512), default="", nullable=False)

    start_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    end_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Native Google timezone string (e.g. "Asia/Kolkata"). Preserved so
    # we can re-render in the user's intended tz even if the DB row is
    # stored as UTC.
    tz: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    is_all_day: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Attendees: JSON-encoded list of {email, displayName, response}
    # kept as TEXT so we don't need JSONB for a flat list. Read via
    # json.loads in the store layer.
    attendees_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)

    # Google Meet / Zoom / phone links — first non-empty entry wins.
    meet_link: Mapped[str] = mapped_column(String(512), default="", nullable=False)

    # "confirmed" / "tentative" / "cancelled" — we soft-delete by flipping
    # status to 'cancelled' rather than removing the row (preserves history).
    status: Mapped[str] = mapped_column(
        String(16), default="confirmed", nullable=False, index=True
    )

    # Who created it, who's the organizer — useful for briefings.
    organizer_email: Mapped[str] = mapped_column(
        String(256), default="", nullable=False
    )
    creator_email: Mapped[str] = mapped_column(
        String(256), default="", nullable=False
    )

    # Google's etag — used for cheap change detection on the harvester.
    etag: Mapped[str] = mapped_column(String(256), default="", nullable=False)

    # When Astra first saw / last synced this event.
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    last_synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
