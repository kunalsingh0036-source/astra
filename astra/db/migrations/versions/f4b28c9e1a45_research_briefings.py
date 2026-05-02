"""research_briefings

Stores every intel briefing Research Intel produces — on-demand via
the `research` MCP tool, or scheduled daily at 07:00 IST.

Kind:
  on_demand    — Kunal asked Astra to research X
  scheduled    — the 7am job picked this topic from the rotating queue
  from_meeting — triggered by a meeting summary that flagged a thing
                 worth deeper investigation

Status lifecycle:
  pending      — row inserted, agent hasn't run yet
  running      — sub-agent kicked off
  ready        — body_md populated, signals + action_items stored
  error        — agent failed; see `error` column

Revision ID: f4b28c9e1a45
Revises: e2a41b8c7f33
Create Date: 2026-04-20
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "f4b28c9e1a45"
down_revision: Union[str, Sequence[str], None] = "e2a41b8c7f33"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "research_briefings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "topic", sa.String(length=512), nullable=False, index=True,
        ),
        sa.Column(
            "kind",
            sa.String(length=16),
            nullable=False,
            server_default="on_demand",
        ),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="pending",
            index=True,
        ),
        sa.Column("body_md", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "signals",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "action_items",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "sources",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "business_tags",
            sa.String(length=256),
            nullable=False,
            server_default="",
        ),
        sa.Column(
            "memory_id",
            sa.Integer(),
            nullable=True,
        ),
        sa.Column(
            "task_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("model_used", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("research_briefings")
