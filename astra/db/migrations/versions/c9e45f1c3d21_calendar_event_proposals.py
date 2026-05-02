"""calendar_event_proposals

Approval-gated staging table for calendar writes. Every create / update /
delete Astra wants to perform on Google Calendar lands here first as a
pending row; Kunal clicks Apply; a 60-s worker performs the API call and
flips status to 'applied'.

Mirrors the calendar_events fields but adds:
  - kind:        create | update | delete
  - recurrence:  RRULE string for weekly-scaffold events (or NULL)
  - google_id:   the target event id for update / delete (NULL on create)
  - resulting_google_id: the id returned after a successful create
  - status machine: pending | approved | applied | rejected | expired | error

Revision ID: c9e45f1c3d21
Revises: b8d32a1f0e90
Create Date: 2026-04-19
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c9e45f1c3d21"
down_revision: Union[str, Sequence[str], None] = "b8d32a1f0e90"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "calendar_event_proposals",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "kind",
            sa.String(length=16),
            nullable=False,
            server_default="create",
        ),
        sa.Column(
            "source",
            sa.String(length=64),
            nullable=False,
            server_default="manual",
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
        # Google RRULE (e.g. "RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR") or NULL
        # for one-off events. Full recurrence_json supports more complex cases.
        sa.Column("recurrence_json", sa.Text(), nullable=True),
        # For update/delete — the target Google event id.
        sa.Column("google_id", sa.String(length=256), nullable=True, index=True),
        # After a successful create, we stash the new google id here so
        # future update/delete proposals can reference it.
        sa.Column(
            "resulting_google_id",
            sa.String(length=256),
            nullable=True,
        ),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_calendar_event_proposals_status_created",
        "calendar_event_proposals",
        ["status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_calendar_event_proposals_status_created",
        table_name="calendar_event_proposals",
    )
    op.drop_table("calendar_event_proposals")
