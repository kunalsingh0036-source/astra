"""shares + share_tokens

`shares`        — every payload dropped into Astra via the iOS Share
                  Sheet extension. Text, URL, image, or a mix.
`share_tokens`  — pairing tokens issued by /settings/share to the
                  phone. One token per device; revocable.

State machine for shares:
  received   — just landed, pipeline not yet decided what to do
  processing — Claude is classifying + routing
  filed      — became a memory / task / draft / note
  error      — pipeline failed (see `error`)

Revision ID: h6a91d8e2b53
Revises: g5f82a1c9e47
Create Date: 2026-04-22
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "h6a91d8e2b53"
down_revision: Union[str, Sequence[str], None] = "g5f82a1c9e47"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "share_tokens",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("token", sa.String(length=128), nullable=False, unique=True, index=True),
        sa.Column("device_label", sa.String(length=256), nullable=False, server_default=""),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="active",
            index=True,
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "shares",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "token_id",
            sa.Integer(),
            sa.ForeignKey("share_tokens.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "kind",
            sa.String(length=16),
            nullable=False,
            server_default="text",
        ),
        sa.Column("source_app", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("source_url", sa.Text(), nullable=False, server_default=""),
        sa.Column("title", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("text", sa.Text(), nullable=False, server_default=""),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        # Image payloads get stored on disk and we reference the path.
        # Keeps the DB lean and lets the meetings whisper.cpp pipeline
        # pick up audio shares without special-casing.
        sa.Column("file_path", sa.Text(), nullable=False, server_default=""),
        sa.Column("mime_type", sa.String(length=128), nullable=False, server_default=""),
        sa.Column(
            "state",
            sa.String(length=16),
            nullable=False,
            server_default="received",
            index=True,
        ),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "action_taken",
            sa.String(length=64),
            nullable=False,
            server_default="",
        ),
        sa.Column("memory_id", sa.Integer(), nullable=True),
        sa.Column(
            "task_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            index=True,
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("shares")
    op.drop_table("share_tokens")
