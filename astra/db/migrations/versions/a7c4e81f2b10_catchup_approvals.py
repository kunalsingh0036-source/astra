"""catchup_approvals

Stages each 21:30 training catch-up reply as a pending approval row.
The 22:00 briefing inserts a row per parsed reply; an Apply job picks
up rows in state='approved' and mutates the Kunal Apple Note.

States:
  pending  — parsed, waiting for Kunal's green light
  approved — Kunal clicked Approve; the Apply job will pick it up
  applied  — writeback succeeded; note counters decremented
  rejected — Kunal declined
  expired  — >24h old without an answer; writeback never runs
  error    — writeback attempted but failed (see error text)

Revision ID: a7c4e81f2b10
Revises: ed9905755944
Create Date: 2026-04-19
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "a7c4e81f2b10"
down_revision: Union[str, Sequence[str], None] = "ed9905755944"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "catchup_approvals",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        # reply_id is the Gmail message id (or fallback marker). Unique
        # so two briefings can't double-stage the same reply.
        sa.Column("reply_id", sa.String(length=256), nullable=False, unique=True),
        sa.Column(
            "decrements", postgresql.JSONB(astext_type=sa.Text()), nullable=False,
        ),
        sa.Column(
            "before_counters",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "projected_after",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("hours_reported", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
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
        "ix_catchup_approvals_status_created",
        "catchup_approvals",
        ["status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_catchup_approvals_status_created", table_name="catchup_approvals"
    )
    op.drop_table("catchup_approvals")
