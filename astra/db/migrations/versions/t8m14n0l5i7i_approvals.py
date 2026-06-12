"""approvals + tool_grants + autonomy_mode_history — trust staging.

The autonomy system's ASK decision finally gets a real mechanism.
Before this, the lean runtime silently mapped ASK→ALLOW for
read/write tools and ASK→DENY for destructive ones — `always_ask`
mode was a fiction (deep-scan P1 #7).

Three tables:

`approvals` — one row per ASK decision. The runtime creates it,
returns a "waiting for your approval" tool_result (non-blocking:
turns never hang on a human), and the model relays. Kunal resolves
via the /approvals page, the resolve_approval chat tool, or
WhatsApp. An approved row is a one-shot grant consumed by the next
identical call; "standing" approvals also write tool_grants.

`tool_grants` — per-tool standing auto-allow, earned through real
approvals. This is the trust ladder: everything starts at ask,
individual tools get promoted based on Kunal's actual yes/no
history (resolution_source records where each decision came from).

`autonomy_mode_history` — the calendared 2026-06-06 item: mode
transitions persisted cross-service instead of living in a
per-process list.

Revision ID: t8m14n0l5i7i
Revises: s7l03m9k4h6h
Create Date: 2026-06-12
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "t8m14n0l5i7i"
down_revision: Union[str, Sequence[str], None] = "s7l03m9k4h6h"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "approvals",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("turn_id", sa.Integer(), nullable=True, index=True),
        sa.Column("session_id", sa.Text(), nullable=True),
        sa.Column("tool_name", sa.String(length=128), nullable=False),
        sa.Column("tool_input", JSONB(), nullable=False, server_default="{}"),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="pending",
        ),  # pending | approved | denied | expired | consumed
        sa.Column(
            "standing",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "resolution_source", sa.String(length=32), nullable=True
        ),  # web | chat | whatsapp
    )
    op.create_index(
        "ix_approvals_status_created",
        "approvals",
        ["status", "created_at"],
    )

    op.create_table(
        "tool_grants",
        sa.Column("tool_name", sa.String(length=128), primary_key=True),
        sa.Column(
            "granted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("source", sa.String(length=32), nullable=False, server_default="web"),
        sa.Column("approval_id", sa.Integer(), nullable=True),
    )

    op.create_table(
        "autonomy_mode_history",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("from_mode", sa.String(length=32), nullable=False),
        sa.Column("to_mode", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("source", sa.String(length=64), nullable=False, server_default=""),
        sa.Column(
            "at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def downgrade() -> None:
    op.drop_table("autonomy_mode_history")
    op.drop_table("tool_grants")
    op.drop_index("ix_approvals_status_created", table_name="approvals")
    op.drop_table("approvals")
