"""
Persistent audit event model.

Every autonomy decision (allow / deny / ask) writes one row here so
/audit in astra-web can render the full trust trail, not just what's
in the current process memory.
"""

from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from astra.db.engine import Base


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
        index=True,
    )
    tool_name: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    action_tier: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    autonomy_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    decision: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    tool_input_summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    context: Mapped[str] = mapped_column(Text, default="", nullable=False)
