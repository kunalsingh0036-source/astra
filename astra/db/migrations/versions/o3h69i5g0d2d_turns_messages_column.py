"""turns.messages JSONB — full conversation history for lean runtime

The lean runtime (Phase 4) loads prior turns' assistant content
(including tool_use blocks) and tool_result content to reconstruct
the multi-turn message stack the Anthropic Messages API expects.

The legacy turns table only stored the final assistant TEXT
(`response` column). For session continuity the lean runtime needs
the full message structure — assistant turns may contain text +
tool_use blocks, user turns may contain tool_result blocks. JSONB
keeps the schema flexible without per-shape DDL.

Default `'[]'::jsonb` so existing rows are valid (no backfill).

Revision ID: o3h69i5g0d2d
Revises: n2g58h4f9c1c
Create Date: 2026-05-04
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "o3h69i5g0d2d"
down_revision: Union[str, Sequence[str], None] = "n2g58h4f9c1c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "turns",
        sa.Column(
            "messages",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("turns", "messages")
