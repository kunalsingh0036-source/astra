"""local bridge — bridge_tokens + bridge_calls tables

Phase 7 of the runtime work — gives Astra (running on Railway) the
ability to operate on Kunal's Mac. A small daemon process runs on
the Mac, long-polls Railway for pending tool calls, executes them
against the local filesystem / shell, and posts results back.

Two tables:

`bridge_tokens` — durable credentials a Mac daemon presents to
authenticate. Each token carries:
  - allowed_paths: JSON array of root directories the daemon is
    permitted to read/write/exec under. Outside-of-allowlist
    requests are refused at the daemon, never reach the disk.
  - allowed_bash_patterns: optional whitelist of shell-command
    regex prefixes. NULL = any command is allowed (still gated
    by autonomy tier on the Astra side).
  - label: short human-readable name ("kunal-mbp", "studio-mac")
    so the audit page reads cleanly.

`bridge_calls` — per-invocation queue rows. The flow is:
  1. An Astra tool (local_read, local_bash, ...) inserts a row
     with status='pending', tool_name, args, the user_token to
     route to. Returns the row id.
  2. The daemon long-polls /bridge/poll, sees the row, sets
     status='running', picks_up_at = now().
  3. Daemon executes the action locally, POSTs to /bridge/result
     with row id + result + ok flag.
  4. Server flips status='complete' (or 'failed') with result
     stored. The waiting Astra tool sees the update and returns.

Per-tool timeouts on the Astra side cap how long we wait — if the
daemon is offline or the call hangs, the tool returns is_error
rather than blocking the agent forever.

Revision ID: p4i70j6h1e3e
Revises: o3h69i5g0d2d
Create Date: 2026-05-04
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "p4i70j6h1e3e"
down_revision: Union[str, Sequence[str], None] = "o3h69i5g0d2d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── bridge_tokens ────────────────────────────────────────
    op.create_table(
        "bridge_tokens",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        # SHA-256 hex of the actual token string. The plaintext is shown
        # to the user ONCE at creation; only the hash is stored.
        sa.Column("token_hash", sa.String(length=64), nullable=False, unique=True),
        sa.Column("label", sa.String(length=128), nullable=False, server_default=""),
        # Roots the daemon is allowed to operate under. JSON array of
        # absolute paths. The daemon resolves every requested path to
        # its real location and refuses anything that doesn't sit
        # under one of these prefixes.
        sa.Column(
            "allowed_paths",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        # Optional bash whitelist — list of regex patterns. NULL/empty
        # means autonomy-tier gating is the only check (DESTRUCTIVE
        # tier already requires approval).
        sa.Column(
            "allowed_bash_patterns",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )

    # ── bridge_calls ─────────────────────────────────────────
    op.create_table(
        "bridge_calls",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "bridge_token_id",
            sa.Integer(),
            sa.ForeignKey("bridge_tokens.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("tool_name", sa.String(length=64), nullable=False),
        sa.Column(
            "args",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("result", sa.Text(), nullable=True),
        # 'pending' | 'running' | 'complete' | 'failed' | 'timeout'
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("picked_up_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_bridge_calls_status", "bridge_calls", ["status"])
    op.create_index("ix_bridge_calls_created_at", "bridge_calls", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_bridge_calls_created_at", table_name="bridge_calls")
    op.drop_index("ix_bridge_calls_status", table_name="bridge_calls")
    op.drop_table("bridge_calls")
    op.drop_table("bridge_tokens")
