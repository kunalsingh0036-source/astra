"""calendar_events

Mirrors Kunal's Google Calendar (primary cal) on a 10-minute refresh.
Backing table for the briefing's "what's on tomorrow" signal and the
calendar_* MCP tools. Write path is not yet wired; when it is, it'll
go through the approval-gated pattern (same as notes/writeback).

Revision ID: b8d32a1f0e90
Revises: a7c4e81f2b10
Create Date: 2026-04-19
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b8d32a1f0e90"
down_revision: Union[str, Sequence[str], None] = "a7c4e81f2b10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "calendar_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "google_id", sa.String(length=256), nullable=False, unique=True
        ),
        sa.Column(
            "calendar_id",
            sa.String(length=256),
            nullable=False,
            server_default="primary",
        ),
        sa.Column(
            "summary", sa.String(length=512), nullable=False, server_default=""
        ),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "location", sa.String(length=512), nullable=False, server_default=""
        ),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "tz", sa.String(length=64), nullable=False, server_default=""
        ),
        sa.Column(
            "is_all_day",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "attendees_json", sa.Text(), nullable=False, server_default="[]"
        ),
        sa.Column(
            "meet_link", sa.String(length=512), nullable=False, server_default=""
        ),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="confirmed",
        ),
        sa.Column(
            "organizer_email",
            sa.String(length=256),
            nullable=False,
            server_default="",
        ),
        sa.Column(
            "creator_email",
            sa.String(length=256),
            nullable=False,
            server_default="",
        ),
        sa.Column(
            "etag", sa.String(length=256), nullable=False, server_default=""
        ),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_synced_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_calendar_events_start_at", "calendar_events", ["start_at"]
    )
    op.create_index(
        "ix_calendar_events_calendar_id", "calendar_events", ["calendar_id"]
    )
    op.create_index(
        "ix_calendar_events_status", "calendar_events", ["status"]
    )


def downgrade() -> None:
    op.drop_index("ix_calendar_events_status", table_name="calendar_events")
    op.drop_index("ix_calendar_events_calendar_id", table_name="calendar_events")
    op.drop_index("ix_calendar_events_start_at", table_name="calendar_events")
    op.drop_table("calendar_events")
