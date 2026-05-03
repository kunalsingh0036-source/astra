"""app_settings table — shared key-value store across web + stream services.

The first use case: cross-service autonomy mode persistence. The
file-based approach broke on Railway because each container has its
own ephemeral filesystem; a value written by /api/autonomy in the web
service was invisible to runner.py in the stream service.

Postgres is already shared across all Astra services. A simple
key/value table beats Redis for this kind of low-write-rate config
state because:
  - one source of truth (the same DB the rest of Astra uses)
  - no extra dependency on web's Next.js runtime
  - audit-friendly (updated_at is enough; if we ever need history,
    we add a settings_history table without changing the read path)

Schema is intentionally generic so future settings (theme overrides,
scheduled-task pauses, feature flags) can land in the same table
without another migration.

Revision ID: m1f47g3e8f0b
Revises: l0e36f2d7e9a
Create Date: 2026-05-03
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "m1f47g3e8f0b"
down_revision: Union[str, Sequence[str], None] = "l0e36f2d7e9a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(length=128), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    # Seed the autonomy mode with the safe default so the first read
    # never returns null. The web POST handler upserts on every change.
    op.execute(
        "INSERT INTO app_settings (key, value) VALUES "
        "('autonomy_mode', 'semi_auto') "
        "ON CONFLICT (key) DO NOTHING"
    )


def downgrade() -> None:
    op.drop_table("app_settings")
