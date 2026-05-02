"""usage_events, audit_events, tasks

Adds three tables that were previously created via
`Base.metadata.create_all()` during development:

  - usage_events  (one row per agent turn — cost + tokens)
  - audit_events  (one row per tool permission decision)
  - tasks         (the user's flat to-do list)

Existing dev databases should be stamped to this revision; fresh
environments run upgrade() to build the full schema.

Revision ID: ed9905755944
Revises: 990433cfaf09
Create Date: 2026-04-18
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "ed9905755944"
down_revision: Union[str, Sequence[str], None] = "990433cfaf09"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── usage_events ────────────────────────────────────────
    op.create_table(
        "usage_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "ts",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("session_id", sa.String(length=128), nullable=True),
        sa.Column("subtype", sa.String(length=64), nullable=True),
        sa.Column("stop_reason", sa.String(length=64), nullable=True),
        sa.Column("models", sa.Text(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "cache_read_tokens",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "cache_creation_tokens",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("num_turns", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_error", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "source",
            sa.String(length=32),
            nullable=False,
            server_default="chat",
        ),
    )
    op.create_index("ix_usage_events_ts", "usage_events", ["ts"])
    op.create_index("ix_usage_events_session_id", "usage_events", ["session_id"])
    op.create_index("ix_usage_events_source", "usage_events", ["source"])

    # ── audit_events ────────────────────────────────────────
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "ts",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("tool_name", sa.String(length=256), nullable=False),
        sa.Column("action_tier", sa.String(length=32), nullable=False),
        sa.Column("autonomy_mode", sa.String(length=32), nullable=False),
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.Column(
            "tool_input_summary",
            sa.Text(),
            nullable=False,
            server_default="",
        ),
        sa.Column("context", sa.Text(), nullable=False, server_default=""),
    )
    op.create_index("ix_audit_events_ts", "audit_events", ["ts"])
    op.create_index("ix_audit_events_tool_name", "audit_events", ["tool_name"])
    op.create_index("ix_audit_events_action_tier", "audit_events", ["action_tier"])
    op.create_index("ix_audit_events_decision", "audit_events", ["decision"])

    # ── tasks ───────────────────────────────────────────────
    op.create_table(
        "tasks",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
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
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="open",
        ),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("tags", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "source",
            sa.String(length=32),
            nullable=False,
            server_default="user",
        ),
    )
    op.create_index("ix_tasks_created_at", "tasks", ["created_at"])
    op.create_index("ix_tasks_due_at", "tasks", ["due_at"])
    op.create_index("ix_tasks_status", "tasks", ["status"])


def downgrade() -> None:
    op.drop_index("ix_tasks_status", table_name="tasks")
    op.drop_index("ix_tasks_due_at", table_name="tasks")
    op.drop_index("ix_tasks_created_at", table_name="tasks")
    op.drop_table("tasks")

    op.drop_index("ix_audit_events_decision", table_name="audit_events")
    op.drop_index("ix_audit_events_action_tier", table_name="audit_events")
    op.drop_index("ix_audit_events_tool_name", table_name="audit_events")
    op.drop_index("ix_audit_events_ts", table_name="audit_events")
    op.drop_table("audit_events")

    op.drop_index("ix_usage_events_source", table_name="usage_events")
    op.drop_index("ix_usage_events_session_id", table_name="usage_events")
    op.drop_index("ix_usage_events_ts", table_name="usage_events")
    op.drop_table("usage_events")
