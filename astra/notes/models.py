"""
Apple Notes model — one row per note, synced from the Notes app
via osascript on a schedule. Mirrors Notes faithfully; interpretation
(missed-session extraction, entity mentions, etc.) happens on top.
"""

from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from astra.db.engine import Base


class AppleNote(Base):
    __tablename__ = "apple_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # The AppleScript `id` of the note — stable across rename/move. Used
    # as the upsert key.
    apple_id: Mapped[str] = mapped_column(String(256), unique=True, index=True, nullable=False)

    title: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    folder: Mapped[str] = mapped_column(String(256), default="", nullable=False, index=True)

    # Raw HTML body as returned by Notes.app. Preserves formatting but
    # verbose (can include base64 images). Kept for fidelity.
    body_html: Mapped[str] = mapped_column(Text, default="", nullable=False)

    # HTML-stripped, images-removed plain text. This is what Astra reads
    # and embeds — no base64 junk, no markup noise.
    body_text: Mapped[str] = mapped_column(Text, default="", nullable=False)

    # Hash of body_text — used to detect changes on re-sync so we don't
    # re-embed unchanged notes.
    content_hash: Mapped[str] = mapped_column(String(64), default="", nullable=False, index=True)

    # Native Notes timestamps.
    created_at_native: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    modified_at_native: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    # When Astra first saw / last synced this note.
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

    # Free-form tags for Kunal's organization (e.g. "training", "private").
    # Can be populated later by an extraction step.
    tags: Mapped[str] = mapped_column(Text, default="", nullable=False)

    # Character count of body_text — cheap signal for filtering /
    # prioritizing notes in briefings.
    char_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
