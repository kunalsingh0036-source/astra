"""training_counters — cloud source of truth for the 6 missed-session debt counters

The 6 counters (stretch/meditate/breathe/movement/skill/workout) lived in
the "Kunal" Apple Note and only reached the cloud when the Mac was awake +
the bridge synced — so training context went STALE whenever the laptop was
off (the macOS fault-line gating the Olympic-compass training tracker).

This table makes the CLOUD the source of truth: Kunal updates the counters
over WhatsApp (the log_training tool), `current_counters()` reads here first,
and the daily snapshot + trend + "Kunal Now" stay live regardless of the
Mac. Single-row (id=1); the Apple Note becomes a bootstrap/fallback only.

IF NOT EXISTS so it's a safe no-op if pre-created at point-of-use (the
ensure_training_table guard) and a constructive create on a fresh DB.

Revision ID: u9n25o1m6j8j
Revises: t8m14n0l5i7i
Create Date: 2026-06-29
"""
from typing import Sequence, Union

from alembic import op

revision: str = "u9n25o1m6j8j"
down_revision: Union[str, Sequence[str], None] = "t8m14n0l5i7i"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS training_counters (
            id            INTEGER PRIMARY KEY DEFAULT 1,
            stretch       INTEGER,
            meditate      INTEGER,
            breathe       INTEGER,
            movement      INTEGER,
            skill         INTEGER,
            workout       INTEGER,
            updated_via   TEXT NOT NULL DEFAULT '',
            last_note     TEXT NOT NULL DEFAULT '',
            updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT training_counters_singleton CHECK (id = 1)
        )
    """)


def downgrade() -> None:
    # Holds the live training-counter state. Operator must back up first.
    op.execute("DROP TABLE IF EXISTS training_counters")
