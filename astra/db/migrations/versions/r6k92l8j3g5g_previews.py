"""previews — store agent-generated content (HTML, file body) for
deep-link viewing in a new tab.

The agent calls prepare_preview(content, title, content_type) →
gets back a preview_id → emits a `preview` artifact whose UI surface
shows an inline sandboxed iframe + an "open in tab" button. The
new tab hits GET /api/preview/<id>, which proxies to the stream
service's GET /previews/<id>, which serves the body with the
correct Content-Type.

Why durable storage (not in-memory):
- Survives stream-service restarts; the link works tomorrow too.
- Polling client may load the artifact for the first time minutes
  after the agent emitted it (bridged by the durable turn_events
  log).
- The user can bookmark a preview URL; we want it to still work.

Schema:
  id            uuid PK (URL-friendly)
  title         text — human label shown in the tab + chip
  content_type  varchar(64) — e.g. "text/html; charset=utf-8"
  body          text — the rendered content. HTML for kind=html;
                also supports plain text, markdown, JSON. Capped
                at ~10MB by the API layer (we don't enforce here).
  created_at    timestamptz default now()
  expires_at    timestamptz — TTL. The route returns 410 Gone past
                this. A background sweep can hard-delete; the route
                works correctly without sweeping.

Revision ID: r6k92l8j3g5g
Revises: q5j81k7i2f4f
Create Date: 2026-05-06
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "r6k92l8j3g5g"
down_revision: Union[str, Sequence[str], None] = "q5j81k7i2f4f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "previews",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=False),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("title", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "content_type",
            sa.String(length=64),
            nullable=False,
            server_default="text/html; charset=utf-8",
        ),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_previews_expires_at",
        "previews",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_previews_expires_at", table_name="previews")
    op.drop_table("previews")
