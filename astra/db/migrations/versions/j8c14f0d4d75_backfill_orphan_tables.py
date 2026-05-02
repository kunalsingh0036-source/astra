"""backfill apple_notes + missed_session_snapshots schema

These two tables exist in production (Railway Postgres) because they
were created out-of-band via raw `CREATE TABLE` SQL during early dev,
before the alembic migration discipline was established. They are
NOT in alembic history. A rebuild-from-scratch (or a fresh dev DB)
would not have these tables, breaking apple_notes sync + the missed-
session pipeline.

This revision backfills both with `IF NOT EXISTS` semantics so it's a
safe no-op against production (where the tables exist) AND a
constructive create against any fresh DB.

Note: `astra_scheduler_jobs` is intentionally NOT backfilled here —
APScheduler creates it itself on first scheduler boot, with column
shapes that depend on the apscheduler version. Codifying that table
in alembic would create a maintenance trap.

Resolves task #44.

Revision ID: j8c14f0d4d75
Revises: i7b02f9d3c64
Create Date: 2026-05-02
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "j8c14f0d4d75"
down_revision: Union[str, Sequence[str], None] = "i7b02f9d3c64"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # asyncpg uses prepared statements which only accept ONE SQL
    # statement per execute() call. Split each DDL into its own
    # op.execute(); IF NOT EXISTS keeps each one idempotent against
    # the already-deployed production DB.
    op.execute("""
        CREATE TABLE IF NOT EXISTS apple_notes (
            id              SERIAL PRIMARY KEY,
            apple_id        VARCHAR(256) NOT NULL,
            title           VARCHAR(512) NOT NULL,
            folder          VARCHAR(256) NOT NULL,
            body_html       TEXT NOT NULL,
            body_text       TEXT NOT NULL,
            content_hash    VARCHAR(64) NOT NULL,
            created_at_native  TIMESTAMPTZ,
            modified_at_native TIMESTAMPTZ,
            first_seen_at   TIMESTAMPTZ NOT NULL,
            last_synced_at  TIMESTAMPTZ NOT NULL,
            tags            TEXT NOT NULL DEFAULT '',
            char_count      INTEGER NOT NULL DEFAULT 0
        )
    """)
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_apple_notes_apple_id ON apple_notes (apple_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_apple_notes_content_hash ON apple_notes (content_hash)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_apple_notes_folder ON apple_notes (folder)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_apple_notes_modified_at_native ON apple_notes (modified_at_native)")

    op.execute("""
        CREATE TABLE IF NOT EXISTS missed_session_snapshots (
            id              SERIAL PRIMARY KEY,
            snapshot_date   DATE NOT NULL UNIQUE,
            stretch         INTEGER,
            meditate        INTEGER,
            breathe         INTEGER,
            movement        INTEGER,
            skill           INTEGER,
            workout         INTEGER,
            raw_missing     TEXT DEFAULT '',
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_missed_snapshots_date ON missed_session_snapshots (snapshot_date DESC)")


def downgrade() -> None:
    # Down-direction is destructive — drop only with explicit data
    # protection. These tables hold user data (Apple Notes content +
    # missed-training-session log). Operator must back up first.
    op.execute("DROP INDEX IF EXISTS ix_missed_snapshots_date")
    op.execute("DROP TABLE IF EXISTS missed_session_snapshots")
    op.execute("DROP INDEX IF EXISTS ix_apple_notes_modified_at_native")
    op.execute("DROP INDEX IF EXISTS ix_apple_notes_folder")
    op.execute("DROP INDEX IF EXISTS ix_apple_notes_content_hash")
    op.execute("DROP INDEX IF EXISTS ix_apple_notes_apple_id")
    op.execute("DROP TABLE IF EXISTS apple_notes")
