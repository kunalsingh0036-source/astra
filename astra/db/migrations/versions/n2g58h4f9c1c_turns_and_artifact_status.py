"""turns table + creator_artifacts status column

Two related changes that together fix "I refreshed mid-research and
lost everything" at the structural level:

1. NEW TABLE `turns` — one row per chat turn, written by services/stream/
   runner.py. Captures (prompt, response, status, duration, cost,
   session_id) so even if the SSE stream dies between turn-start and
   turn-end, the prompt is preserved AND any partial response written
   so far is recoverable. Replaces "the conversation is gone after
   refresh" with "the conversation lives in Postgres; the browser is
   just one view of it."

   status values:
     - 'running'      → turn started, no `done` event yet
     - 'complete'     → SDK returned ResultMessage cleanly
     - 'failed'       → SDK raised inside the loop
     - 'interrupted'  → cleanup job marks abandoned 'running' rows
       (not implemented yet — placeholder for the eventual sweeper)

2. NEW COLUMN `creator_artifacts.status` — multi-step creator tools
   (analyze_reference_site, draft_brand_kit, etc.) now write a
   placeholder row at the START of work so the URL + structural data
   are persisted even if the LLM call later fails. Default is
   'complete' so existing rows behave as before.

Revision ID: n2g58h4f9c1c
Revises: m1f47g3e8f0b
Create Date: 2026-05-03
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "n2g58h4f9c1c"
down_revision: Union[str, Sequence[str], None] = "m1f47g3e8f0b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── 1. turns table ──────────────────────────────────────
    op.create_table(
        "turns",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        # Session id from the Claude Agent SDK. Multiple turns share
        # the same session_id when the user is continuing a thread.
        # Indexed so we can fetch "all turns for session X".
        sa.Column("session_id", sa.String(length=128), nullable=True),
        # The user-facing prompt — what Kunal typed. Kept raw so the
        # /audit page can show exactly what was asked.
        sa.Column("prompt", sa.Text(), nullable=False),
        # The agent's full text response. NULL while running; populated
        # when the turn completes. Capped at ~256KB by the renderer
        # before write to keep individual rows reasonable.
        sa.Column("response", sa.Text(), nullable=True),
        # 'running' | 'complete' | 'failed' | 'interrupted'
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="running",
        ),
        sa.Column("tool_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Numeric(10, 6), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_turns_session_id", "turns", ["session_id"])
    op.create_index("ix_turns_started_at", "turns", ["started_at"])
    op.create_index("ix_turns_status", "turns", ["status"])

    # ── 2. creator_artifacts.status ─────────────────────────
    # Default 'complete' so existing rows (which represent finished
    # work) remain valid without a backfill UPDATE.
    op.add_column(
        "creator_artifacts",
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="complete",
        ),
    )
    op.create_index(
        "ix_creator_artifacts_status",
        "creator_artifacts",
        ["status"],
    )


def downgrade() -> None:
    op.drop_index("ix_creator_artifacts_status", table_name="creator_artifacts")
    op.drop_column("creator_artifacts", "status")

    op.drop_index("ix_turns_status", table_name="turns")
    op.drop_index("ix_turns_started_at", table_name="turns")
    op.drop_index("ix_turns_session_id", table_name="turns")
    op.drop_table("turns")
