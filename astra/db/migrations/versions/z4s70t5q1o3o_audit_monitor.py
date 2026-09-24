"""Latest independent audit checks and durable notification deduplication.

Revision ID: z4s70t5q1o3o
Revises: y3r69s4p0n2n

Operational checkpoints only, NOT an authoritative audit or permission.
The independently signed objects remain in R2. No credentials, record
contents, remote error messages or arbitrary strings are persisted here.
"""
from alembic import op

revision = "z4s70t5q1o3o"
down_revision = "y3r69s4p0n2n"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SET lock_timeout = '5s'")
    op.execute("""
        CREATE TABLE audit_verifications (
            check_kind TEXT PRIMARY KEY CHECK (check_kind IN ('verify', 'freshness')),
            chain_id TEXT NOT NULL,
            checked_at TIMESTAMPTZ NOT NULL,
            state TEXT NOT NULL CHECK (state IN ('ok', 'failed', 'deferred', 'unconfigured')),
            verdict TEXT NOT NULL,
            record_count BIGINT NOT NULL DEFAULT 0,
            head_seq BIGINT,
            alert_key TEXT,
            notified_key TEXT
        )
    """)


def downgrade():
    op.execute("SET lock_timeout = '5s'")
    op.execute("DROP TABLE audit_verifications")
