"""
Task model — one row per actionable item.

Minimal by design: title, optional note, due date, status. We don't
model assignees, projects, labels, subtasks — Astra is single-user and
a flat list with a search bar is enough for now.
"""

from datetime import datetime, timezone
import enum

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from astra.db.engine import Base


class TaskStatus(str, enum.Enum):
    OPEN = "open"
    DONE = "done"
    CANCELLED = "cancelled"


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
        index=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    due_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    title: Mapped[str] = mapped_column(String(512), nullable=False)
    note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # open / done / cancelled — stored as string to avoid Postgres enum
    # churn when we need to add states later.
    status: Mapped[str] = mapped_column(
        String(16), default=TaskStatus.OPEN.value, nullable=False, index=True
    )
    # 0 = none, 1 = normal, 2 = high, 3 = urgent
    priority: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    # Optional tag string, comma-separated. Keeps UX lightweight.
    tags: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # Where the task came from — "user", "chat", "email", "scheduler"…
    source: Mapped[str] = mapped_column(String(32), default="user", nullable=False)
