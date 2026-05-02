"""creator_artifacts table

Holds every artifact Astra's creator sub-agent produces: decks,
docs, one-pagers, drafted brand kits, and critique passes. Each
row carries:

- `business_slug` — which kit was used (helmtech/apex/bay/top-studios
  or a client kit drafted via draft_brand_kit)
- `kind` — deck | doc | one_pager | brand_kit | critique | post
- `audience_slug` — which persona file from audiences/ was loaded
  (nullable; brand_kit drafts have no single audience)
- `title`, `ask` — surface-level metadata for listings
- `content` JSONB — the structured payload (slide list / markdown
  body / kit YAML, depending on `kind`)
- `parent_id` — for critique rows pointing back to the artifact they
  critique; for revisions pointing back to the prior version
- `r2_pdf_key`, `r2_pptx_key` — paths to rendered binaries in
  Cloudflare R2, populated by render_pdf / render_pptx tools

Indexed by business_slug and created_at DESC for the most common
query patterns ("list recent decks for HelmTech").

Revision ID: k9d25e1e5e86
Revises: j8c14f0d4d75
Create Date: 2026-05-02
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "k9d25e1e5e86"
down_revision: Union[str, Sequence[str], None] = "j8c14f0d4d75"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "creator_artifacts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("business_slug", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("audience_slug", sa.String(length=128), nullable=True),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("ask", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "content",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "parent_id",
            sa.Integer(),
            sa.ForeignKey("creator_artifacts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("r2_pdf_key", sa.Text(), nullable=False, server_default=""),
        sa.Column("r2_pptx_key", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_creator_artifacts_business",
        "creator_artifacts",
        ["business_slug"],
    )
    op.create_index(
        "ix_creator_artifacts_kind",
        "creator_artifacts",
        ["kind"],
    )


def downgrade() -> None:
    op.drop_index("ix_creator_artifacts_kind", table_name="creator_artifacts")
    op.drop_index("ix_creator_artifacts_business", table_name="creator_artifacts")
    op.drop_table("creator_artifacts")
