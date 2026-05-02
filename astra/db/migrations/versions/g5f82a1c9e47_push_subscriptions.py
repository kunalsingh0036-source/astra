"""push_subscriptions

Stores Web Push subscriptions — one row per browser/device. Astra sends
to every active row when a notification-worthy event fires.

Lifecycle:
  active        — last send succeeded (or never sent yet)
  gone          — endpoint returned 404/410 from the push service;
                  browser permanently revoked the subscription
  failed        — consecutive send failures but not yet 'gone'

On send:
  - If the push service returns 404 or 410, flip to 'gone'.
  - Otherwise bump last_seen on success; increment failure_count on
    transient errors.

Revision ID: g5f82a1c9e47
Revises: f4b28c9e1a45
Create Date: 2026-04-22
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "g5f82a1c9e47"
down_revision: Union[str, Sequence[str], None] = "f4b28c9e1a45"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "push_subscriptions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "endpoint",
            sa.Text(),
            nullable=False,
            unique=True,
            index=True,
        ),
        sa.Column("p256dh", sa.Text(), nullable=False),
        sa.Column("auth", sa.Text(), nullable=False),
        # Browser / device hints — surfaced to the settings page so
        # Kunal can spot-check and revoke individual devices.
        sa.Column(
            "user_agent", sa.Text(), nullable=False, server_default="",
        ),
        sa.Column(
            "device_label",
            sa.String(length=256),
            nullable=False,
            server_default="",
        ),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="active",
            index=True,
        ),
        sa.Column(
            "failure_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "last_success_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "last_failure_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("push_subscriptions")
