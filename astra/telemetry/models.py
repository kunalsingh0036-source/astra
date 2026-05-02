"""
Telemetry models.

One row per agent turn (ResultMessage from Agent SDK). We store what
the SDK reports directly — no derived fields — so aggregations in
/api/cost stay truthful and easy to audit against provider invoices.
"""

from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from astra.db.engine import Base


class UsageEvent(Base):
    __tablename__ = "usage_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
        index=True,
    )

    # SDK identifiers
    session_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    subtype: Mapped[str | None] = mapped_column(String(64), nullable=True)
    stop_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Models used (comma-joined if multiple in this turn)
    models: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Token counts (aggregated across all models used in the turn)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cache_creation_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # What the SDK says the turn cost. May be None if Anthropic doesn't
    # return it (rare). We keep the raw number — no currency conversion.
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    duration_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    num_turns: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_error: Mapped[bool] = mapped_column(default=False, nullable=False)

    # Which surface triggered this turn — "chat", "scheduler", etc.
    source: Mapped[str] = mapped_column(String(32), default="chat", nullable=False, index=True)
