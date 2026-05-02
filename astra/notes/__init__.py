"""Apple Notes ingestion — Kunal's personal tracking + context source."""

from astra.notes.models import AppleNote
from astra.notes.store import (
    get_note,
    list_notes,
    search_notes,
    upsert_note,
)

__all__ = ["AppleNote", "get_note", "list_notes", "search_notes", "upsert_note"]
