"""Statutory obligations — the deadline engine's two tables.

Why this exists: AOC-4 and MGT-7 late fees are Rs 100/day with NO CAP,
per form, per company. Across Kunal's entities a year's slip is ~Rs 1.5L
in pure penalty, and a late ITR additionally forfeits carry-forward of
business losses — the most expensive deadline a capital-burning company
has. A deterministic calendar makes that class of loss structurally
impossible.

TWO HARD RULES, both enforced in the schema rather than in a prompt:

1. NO UNSOURCED RULE MAY ALERT. statute_ref / source_url / verified_on /
   verified_by are NOT NULL, and a rule whose status != 'verified'
   materialises but is never alerted on — it surfaces in a "needs CA
   sign-off" tray instead. This is the `_guard()` shape from
   astra/research/agent.py ported to money: the fabricated-deadline
   failure mode is removed in code, not discouraged in wording.

2. UNKNOWN IS NEVER "NOT APPLICABLE". If the entity master lacks the
   field a rule predicates on (say gst_registration_type is NULL), the
   obligation is created with status='needs_entity_data' — never
   silently skipped. A missing GSTIN must read as "I cannot tell",
   because "does not apply" and "I don't know" have very different
   costs when one of them is Rs 100/day.
"""

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean, Date, DateTime, ForeignKey, Index, Numeric, String, Text,
    UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from finance.db.engine import Base


class RuleStatus(str, enum.Enum):
    VERIFIED = "verified"        # a human (ideally the CA) signed this off
    UNVERIFIED = "unverified"    # materialises, but NEVER alerts


class ObligationStatus(str, enum.Enum):
    UPCOMING = "upcoming"
    DUE = "due"
    OVERDUE = "overdue"
    FILED = "filed"
    NOT_APPLICABLE = "not_applicable"
    NEEDS_ENTITY_DATA = "needs_entity_data"   # predicate unresolvable


class ObligationRule(Base):
    """The catalogue. Static, seeded, versioned, source-gated."""

    __tablename__ = "obligation_rules"

    code: Mapped[str] = mapped_column(String(40), primary_key=True)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    authority: Mapped[str] = mapped_column(String(20), nullable=False)  # GST|MCA|IT|EPFO|ESIC|STATE|RBI
    cadence: Mapped[str] = mapped_column(String(20), nullable=False)    # monthly|quarterly|annual|event

    # Structured, NOT a string DSL. A hand-rolled parser on the table that
    # decides Rs 100/day exposure is a bug surface with a price tag; a
    # dict of {kind, params} is directly testable and cannot mis-lex.
    due_rule: Mapped[dict] = mapped_column(JSONB, nullable=False)
    applies_when: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    penalty_per_day: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    penalty_capped: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    penalty_note: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # Source gate — all four NOT NULL by design.
    statute_ref: Mapped[str] = mapped_column(String(200), nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    verified_on: Mapped[date] = mapped_column(Date, nullable=False)
    verified_by: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=RuleStatus.UNVERIFIED.value
    )

    owner: Mapped[str] = mapped_column(String(20), nullable=False, default="kunal")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"<ObligationRule({self.code}, {self.status})>"


class Obligation(Base):
    """One instance of a rule, for one entity, for one period."""

    __tablename__ = "obligations"
    __table_args__ = (
        UniqueConstraint("business_id", "rule_code", "period_label", name="uq_obligation_instance"),
        Index("ix_obligations_due", "due_date", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    rule_code: Mapped[str] = mapped_column(
        String(40), ForeignKey("obligation_rules.code", ondelete="CASCADE"), nullable=False
    )
    period_label: Mapped[str] = mapped_column(String(30), nullable=False)
    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default=ObligationStatus.UPCOMING.value
    )

    filed_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    filed_ref: Mapped[str | None] = mapped_column(String(120), nullable=True)  # ARN / SRN / ack no.
    owner: Mapped[str] = mapped_column(String(20), nullable=False, default="kunal")

    # penalty_per_day x days_late, computed by the engine. NEVER by a model.
    exposure_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    # Blackout-shifted: pulled forward for Nationals / the raise window.
    first_alert_on: Mapped[date] = mapped_column(Date, nullable=False)
    alert_note: Mapped[str] = mapped_column(Text, nullable=False, default="")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return f"<Obligation({self.rule_code} {self.period_label} due={self.due_date})>"
