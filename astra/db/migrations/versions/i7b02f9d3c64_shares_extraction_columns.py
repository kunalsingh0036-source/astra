"""shares: extracted_text + retry_count + client_ts

Three columns added so the shares pipeline can:

- `extracted_text`  — cache PDF text / URL body content / OCR (future)
                      so we don't re-extract on every read, and so the
                      research briefing can actually quote it.
- `retry_count`     — how many times the pipeline has tried + failed.
                      Pipeline tick re-attempts state='received' rows
                      with retry_count < 5. Past 5 we mark them error.
- `client_ts`       — timestamp the iOS extension stamped at capture
                      time. Lets the outbox retry path land shares
                      with their original moment, not the moment the
                      network finally cooperated.

Revision ID: i7b02f9d3c64
Revises: h6a91d8e2b53
Create Date: 2026-04-25
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "i7b02f9d3c64"
down_revision: Union[str, Sequence[str], None] = "h6a91d8e2b53"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "shares",
        sa.Column(
            "extracted_text",
            sa.Text(),
            nullable=False,
            server_default="",
        ),
    )
    op.add_column(
        "shares",
        sa.Column(
            "retry_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "shares",
        sa.Column(
            "client_ts",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("shares", "client_ts")
    op.drop_column("shares", "retry_count")
    op.drop_column("shares", "extracted_text")
