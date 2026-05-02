"""meetings

Stores one row per recorded meeting, linked back to calendar_events
when a temporal match exists. Pipeline state machine:

  detected  → file dropped in ~/Astra/recordings/, row inserted
  transcribing → whisper.cpp running
  transcribed → raw transcript ready; awaiting summary
  summarizing → Claude synthesizing
  ready       → transcript + summary + action_items available
  error       → any step failed (see `error` column)

Action items are stored as JSON (title, due_at?, priority?) and are
staged as rows in `tasks` once the meeting reaches 'ready' state.

Revision ID: d1f70a8e4b92
Revises: c9e45f1c3d21
Create Date: 2026-04-19
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "d1f70a8e4b92"
down_revision: Union[str, Sequence[str], None] = "c9e45f1c3d21"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "meetings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "source_path",
            sa.String(length=1024),
            nullable=False,
            unique=True,
        ),
        sa.Column("title", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=True, index=True),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column(
            "state",
            sa.String(length=16),
            nullable=False,
            server_default="detected",
            index=True,
        ),
        sa.Column("transcript", sa.Text(), nullable=False, server_default=""),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "action_items",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "task_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("model_used", sa.String(length=64), nullable=False, server_default=""),
        sa.Column(
            "calendar_event_google_id",
            sa.String(length=256),
            nullable=True,
            index=True,
        ),
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
    )


def downgrade() -> None:
    op.drop_table("meetings")
