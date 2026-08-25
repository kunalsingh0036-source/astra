"""Objectives — goals that pursue themselves.

The gap this fills: Astra could run recurring JOBS (fixed cron) and hold
TASKS (a title and a due date), but it had no way to hold a GOAL and
chase it — "get the term sheet back from Samarth; nudge every 3 days;
escalate to me after two tries; stop the moment he replies."

Three things make this different from a task:
  1. It has a DONE CONDITION that is checked, not assumed.
  2. It has an ESCALATION LADDER — attempts are counted, and running out
     of them is itself an outcome worth telling Kunal about.
  3. It stops on its own. A loop that cannot detect success is a nag,
     and a nag gets muted.

Standing rules this obeys:
  - Draft-don't-send on personal channels. The loop STAGES the nudge; a
    human sends it. Astra never claims to have sent from his accounts.
  - Alert on OUTCOMES, not components. One batched message per tick,
    never one ping per objective.
  - Objectives are LIVE STATE, never memory. The confabulation bug (a
    transient status written as episodic memory and replayed for weeks)
    applies exactly here: "chasing Samarth" must not outlive the chase.
"""

import enum
from datetime import datetime

from sqlalchemy import (
    JSON, DateTime, ForeignKey, Index, Integer, String, Text, func,
)
from sqlalchemy.orm import Mapped, mapped_column

from astra.db.engine import Base


class ObjectiveState(str, enum.Enum):
    ACTIVE = "active"
    DONE = "done"
    ABANDONED = "abandoned"
    PAUSED = "paused"


class Objective(Base):
    __tablename__ = "objectives"
    __table_args__ = (
        Index("ix_objectives_due", "state", "next_check_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    goal: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # How we KNOW it is finished. Checked every tick, never assumed.
    #   {"kind": "email_reply", "from": "samarth@x.com"}
    #   {"kind": "manual"}                      <- Kunal closes it
    #   {"kind": "obligation_filed", "id": "<uuid>"}
    done_when: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    channel: Mapped[str] = mapped_column(String(20), nullable=False, default="none")  # email|whatsapp|none
    target_ref: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    context: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    state: Mapped[str] = mapped_column(String(16), nullable=False, default=ObjectiveState.ACTIVE.value)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=4)
    escalate_after: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    cadence_days: Mapped[int] = mapped_column(Integer, nullable=False, default=3)

    next_check_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_action_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    close_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return f"<Objective({self.id}, {self.state}, attempts={self.attempts})>"


class ObjectiveEvent(Base):
    """Append-only. Every tick's decision and why — so a loop that
    misbehaves can be explained rather than guessed at."""

    __tablename__ = "objective_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    objective_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("objectives.id", ondelete="CASCADE"), nullable=False, index=True
    )
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    action: Mapped[str] = mapped_column(String(24), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    artifact_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
