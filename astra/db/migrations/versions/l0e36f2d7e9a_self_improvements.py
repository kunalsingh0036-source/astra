"""self_improvements table — Layer 4 proactive self-improvement queue.

Holds observations about kit/code/output issues that Astra (or a
scheduled scan) noticed but hasn't acted on yet. Each row tracks:

- `source` — what kind of detector observed this (forbidden_phrase_persisted,
  low_critique_score, user_feedback, test_failure, manual)
- `business_slug`, `artifact_id` — context (nullable for both)
- `observation` — text describing what was noticed
- `severity` — low | medium | high (for prioritization)
- `status` — observed → proposed → approved → applied OR dismissed
- `proposed_action` — text describing the proposal once Astra has
  generated one
- `proposed_tool_calls` — JSONB list of {tool, args} dicts that
  represent the actual edit sequence to make. Astra reviews these
  with the user before approval.
- `applied_commit` — the git SHA after the proposal was applied
- `dismissed_reason` — for status=dismissed
- `resolved_at` — set when the row leaves the active queue

Indexed by status and observed_at for the common queries:
- "what's pending review?" → WHERE status IN ('observed', 'proposed')
- "what was the last issue I dismissed?" → WHERE status='dismissed' ORDER BY resolved_at DESC

Revision ID: l0e36f2d7e9a
Revises: k9d25e1e5e86
Create Date: 2026-05-03
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "l0e36f2d7e9a"
down_revision: Union[str, Sequence[str], None] = "k9d25e1e5e86"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "self_improvements",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("business_slug", sa.String(length=64), nullable=True),
        sa.Column("artifact_id", sa.Integer(), nullable=True),
        sa.Column("observation", sa.Text(), nullable=False),
        sa.Column(
            "severity", sa.String(length=16),
            nullable=False, server_default="medium",
        ),
        sa.Column(
            "status", sa.String(length=32),
            nullable=False, server_default="observed",
        ),
        sa.Column("proposed_action", sa.Text(), nullable=True),
        sa.Column(
            "proposed_tool_calls",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("applied_commit", sa.String(length=64), nullable=True),
        sa.Column("dismissed_reason", sa.Text(), nullable=True),
        sa.Column(
            "observed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_self_improvements_status_observed_at",
        "self_improvements",
        ["status", "observed_at"],
    )
    op.create_index(
        "ix_self_improvements_business_slug",
        "self_improvements",
        ["business_slug"],
    )
    op.create_index(
        "ix_self_improvements_artifact_id",
        "self_improvements",
        ["artifact_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_self_improvements_artifact_id", "self_improvements")
    op.drop_index("ix_self_improvements_business_slug", "self_improvements")
    op.drop_index("ix_self_improvements_status_observed_at", "self_improvements")
    op.drop_table("self_improvements")
