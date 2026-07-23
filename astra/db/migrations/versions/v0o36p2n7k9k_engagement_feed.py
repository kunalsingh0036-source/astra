"""engagement_feed — posts scraped from Kunal's own X/LinkedIn feeds (E1).

E1 of the engagement model (Kunal, 2026-07-22): the Mac-side reader
walks his OPEN, logged-in feeds on a schedule, posts batches here; the
cloud selects candidates, drafts comments in his confirmed voice, and
stages an approval slate. His verdicts over ~2 weeks become the training
signal before any autonomy graduation (E2/E3).

Dedupe on post_url so repeated scrolls upsert metrics instead of
duplicating rows. status: new → slated (comment staged) | skipped;
'engaged' reserved for the posted-confirm loop.

Revision ID: v0o36p2n7k9k
Revises: u9n25o1m6j8j
Create Date: 2026-07-22
"""
from typing import Sequence, Union

from alembic import op

revision: str = "v0o36p2n7k9k"
down_revision: Union[str, Sequence[str], None] = "u9n25o1m6j8j"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS engagement_feed (
            id            BIGSERIAL PRIMARY KEY,
            platform      TEXT NOT NULL,
            post_url      TEXT NOT NULL UNIQUE,
            author        TEXT NOT NULL DEFAULT '',
            author_handle TEXT NOT NULL DEFAULT '',
            text          TEXT NOT NULL DEFAULT '',
            metrics       JSONB NOT NULL DEFAULT '{}'::jsonb,
            status        TEXT NOT NULL DEFAULT 'new',
            batch_id      TEXT NOT NULL DEFAULT '',
            seen_at       TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_engagement_feed_status
        ON engagement_feed (status, seen_at DESC)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS engagement_feed")
