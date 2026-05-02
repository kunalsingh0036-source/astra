"""capture_sessions

Tracks each calendar-triggered recording. One row per (event, session) —
so if a meeting ends and a new one starts later, they're distinct rows.

Lifecycle:
  scheduled  → inserted when calendar trigger matches an upcoming event
  recording  → astra-capture subprocess started, pid stored
  finished   → pid terminated cleanly, file finalized
  failed     → spawn error, TCC denial, subprocess crash
  cancelled  → user deleted the event or rejected the meeting

On 'finished', the output file appears in ~/Astra/recordings/ and the
Phase 1 pipeline picks it up → transcribed → summarized → tasks staged.
The `meeting_id` column links back once Phase 1 inserts the meeting row.

Revision ID: e2a41b8c7f33
Revises: d1f70a8e4b92
Create Date: 2026-04-19
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e2a41b8c7f33"
down_revision: Union[str, Sequence[str], None] = "d1f70a8e4b92"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "capture_sessions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "calendar_event_google_id",
            sa.String(length=256),
            nullable=False,
            index=True,
        ),
        sa.Column("summary", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("planned_start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("planned_end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("output_path", sa.String(length=1024), nullable=False),
        sa.Column("pid", sa.Integer(), nullable=True),
        sa.Column(
            "state",
            sa.String(length=16),
            nullable=False,
            server_default="scheduled",
            index=True,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("meeting_id", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "calendar_event_google_id",
            "planned_start_at",
            name="uq_capture_event_start",
        ),
    )


def downgrade() -> None:
    op.drop_table("capture_sessions")
