"""turn_events — durable per-event log for poll-based chat

Replaces the streaming-SSE model with poll. Every event the agent
yields (session, thought, text_delta, tool_call, tool_result,
artifact, error, done) gets a row here. The browser polls
/api/turns/<id>/events?after=<ord> for new rows.

Why: streaming makes us subject to every duration cap in the path
— Vercel maxDuration, Cloudflare Tunnel idle timeout, intermediate
proxies. Long agent turns (draft_doc + render_doc_pdf, multi-step
research, anything tool-heavy) routinely exceeded 60-100s and got
their stream killed. Polling is immune: the request is short and
finishes fast; the agent runs server-side regardless of whether
the browser is currently polling.

Schema:
  id          bigserial PK
  turn_id     int FK turns(id) — cascade on delete
  ord         int — monotonic event sequence within a turn (1, 2, 3…)
  event_name  varchar(32) — session/thought/text_delta/tool_call/
                tool_result/artifact/error/done
  payload     jsonb — the event's data dict (same shape as SSE
                event_emitter.py outputs minus the wire format)
  created_at  timestamptz default now()

Indexes:
  (turn_id, ord) — primary read pattern: "events for turn X after ord N"
  created_at   — for sweep/retention jobs

Revision ID: q5j81k7i2f4f
Revises: p4i70j6h1e3e
Create Date: 2026-05-05
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "q5j81k7i2f4f"
down_revision: Union[str, Sequence[str], None] = "p4i70j6h1e3e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "turn_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "turn_id",
            sa.Integer(),
            sa.ForeignKey("turns.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ord", sa.Integer(), nullable=False),
        sa.Column("event_name", sa.String(length=32), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # Composite primary read path: events for turn N where ord > M.
        # Postgres handles this via the composite index efficiently.
        sa.UniqueConstraint("turn_id", "ord", name="uq_turn_events_turn_ord"),
    )
    op.create_index(
        "ix_turn_events_turn_ord",
        "turn_events",
        ["turn_id", "ord"],
    )
    op.create_index(
        "ix_turn_events_created_at",
        "turn_events",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_turn_events_created_at", table_name="turn_events")
    op.drop_index("ix_turn_events_turn_ord", table_name="turn_events")
    op.drop_table("turn_events")
