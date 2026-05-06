"""session_titles — intelligent topic titles for chat sessions

Sessions in the UI listed their first prompt as the "title", which
made the page unscannable when first prompts were verbose, technical,
or test scaffolding (e.g. "Call emit_table with columns=[name] ...").
This table stores a Haiku-generated topic title per session — 4 to
8 words, captures what the conversation is about, not what the user
literally typed first.

The generator runs background-async after the FIRST turn of a
session completes. Fallback to truncated first_prompt if the
generator failed or hasn't run yet (the API layer LEFT JOINs).

Schema:
  session_id    text  PK — matches turns.session_id
  title         text  not null — the generated title
  generated_at  timestamptz default now()
  source        text  not null — 'haiku' | 'manual' | 'fallback'

Why a separate table (vs adding a column to turns):
  - Sessions aren't a DB row; they're a logical grouping by
    session_id across the turns table. Adding a "session_title"
    column to turns would be denormalized — which row holds the
    title? First? All? Update on every save?
  - Separate table keeps the session-level metadata cleanly
    addressable; a future session_meta table for last_seen,
    pinned, archived, etc. has the same pattern.

Revision ID: s7l03m9k4h6h
Revises: r6k92l8j3g5g
Create Date: 2026-05-06
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "s7l03m9k4h6h"
down_revision: Union[str, Sequence[str], None] = "r6k92l8j3g5g"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "session_titles",
        sa.Column("session_id", sa.Text(), primary_key=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column(
            "generated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "source",
            sa.String(length=16),
            nullable=False,
            server_default="haiku",
        ),
    )
    op.create_index(
        "ix_session_titles_generated_at",
        "session_titles",
        ["generated_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_session_titles_generated_at", table_name="session_titles"
    )
    op.drop_table("session_titles")
