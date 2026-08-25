"""The obligation engine — deterministic statutory calendar.

ZERO LLM CALLS IN THIS FILE, deliberately and permanently. Every date
here is calendar arithmetic and every rupee is penalty_per_day x days.
A model may narrate what this produces; it may never produce it.

`materialise()` is a pure function of (business, window, rules): same
inputs, same output, no I/O. That makes the highest-consequence logic
in the finance stack directly unit-testable, and it is why the due-date
maths is separated from the persistence in routes/obligations.py.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Literal

# ── Blackout windows ───────────────────────────────────────────────
#
# A deadline landing inside one of these gets alerted EARLY, with a
# reason attached. Twelve lines of code that are the difference between
# a filed return and a Rs 100/day meter running while Kunal is on court.
_BLACKOUTS: list[dict[str, Any]] = [
    {
        "start": date(2026, 11, 10), "end": date(2026, 11, 27),
        "alert_days": 45,
        "note": "file EARLY — Nationals are 16-22 Nov and you will not have bandwidth",
    },
    {
        "start": date(2026, 10, 1), "end": date(2026, 11, 30),
        "alert_days": 30,
        "note": "pulled forward — seed diligence window, books must be clean",
    },
]
_DEFAULT_ALERT_DAYS = 21

UNKNOWN = "unknown"
Applicability = Literal[True, False, "unknown"]


@dataclass(frozen=True)
class MaterialisedObligation:
    rule_code: str
    period_label: str
    due_date: date
    status: str            # 'upcoming' | 'needs_entity_data'
    first_alert_on: date
    alert_note: str
    owner: str


# ── Entity predicate: True / False / UNKNOWN ───────────────────────

def applies_to(rule_applies_when: dict, business: dict) -> Applicability:
    """Does this rule bind this entity?

    Returns UNKNOWN — never False — when the entity master lacks the
    field the rule predicates on. "I don't know" and "does not apply"
    have very different costs; conflating them is how a Rs 100/day
    obligation gets silently skipped.
    """
    if not rule_applies_when:
        return True

    unknown_seen = False
    for key, expected in rule_applies_when.items():
        field = key
        op = "eq"
        for suffix in ("_gte", "_gt", "_in", "_is_true"):
            if key.endswith(suffix):
                field, op = key[: -len(suffix)], suffix.lstrip("_")
                break

        actual = business.get(field)
        if actual is None or actual == "":
            unknown_seen = True
            continue

        if op == "gte" and not (float(actual) >= float(expected)):
            return False
        if op == "gt" and not (float(actual) > float(expected)):
            return False
        if op == "in" and actual not in expected:
            return False
        if op == "is_true" and bool(actual) is not bool(expected):
            return False
        if op == "eq":
            if isinstance(expected, list):
                if actual not in expected:
                    return False
            elif actual != expected:
                return False

    return UNKNOWN if unknown_seen else True


# ── Due-date arithmetic ────────────────────────────────────────────

def _clamp_day(year: int, month: int, day: int) -> date:
    """Day 31 in a 30-day month is the last day, not an exception."""
    last = calendar.monthrange(year, month)[1]
    return date(year, month, min(day, last))


def _shift_month(year: int, month: int, by: int) -> tuple[int, int]:
    idx = (year * 12 + (month - 1)) + by
    return idx // 12, idx % 12 + 1


def _fy_bounds(fy_start_year: int) -> tuple[date, date, str]:
    """Indian FY: 1 Apr fy_start_year -> 31 Mar fy_start_year+1."""
    start = date(fy_start_year, 4, 1)
    end = date(fy_start_year + 1, 3, 31)
    label = f"FY{fy_start_year}-{str(fy_start_year + 1)[2:]}"
    return start, end, label


def due_date_for(due_rule: dict, *, period_anchor: date) -> date | None:
    """Compute the due date. `period_anchor` is the last day of the
    period (month-end / quarter-end / FY-end)."""
    kind = due_rule.get("kind")

    if kind == "monthly_day":
        y, m = _shift_month(period_anchor.year, period_anchor.month,
                            int(due_rule.get("offset_months", 1)))
        return _clamp_day(y, m, int(due_rule["day"]))

    if kind == "quarterly_day":
        y, m = _shift_month(period_anchor.year, period_anchor.month,
                            int(due_rule.get("offset_months", 1)))
        return _clamp_day(y, m, int(due_rule["day"]))

    if kind == "annual_mmdd":
        # Anchored to the CALENDAR YEAR OF FY-END. FY2025-26 ends
        # 2026-03-31, so offset_years=0 + 10-31 => 2026-10-31 (AOC-4).
        y = period_anchor.year + int(due_rule.get("offset_years", 0))
        return _clamp_day(y, int(due_rule["mm"]), int(due_rule["dd"]))

    if kind == "annual_days_after_fy_end":
        return period_anchor + timedelta(days=int(due_rule["days"]))

    # 'event' rules (FC-GPR, PAS-3, MGT-14) fire off a transaction date
    # we do not hold; they are catalogued but never auto-materialised.
    return None


def _alert_plan(due: date) -> tuple[date, str]:
    """first_alert_on + why. Blackout windows pull the alert forward."""
    for window in _BLACKOUTS:
        if window["start"] <= due <= window["end"]:
            return due - timedelta(days=window["alert_days"]), window["note"]
    return due - timedelta(days=_DEFAULT_ALERT_DAYS), ""


# ── The generator ──────────────────────────────────────────────────

def materialise(
    *,
    business: dict,
    rules: list[dict],
    from_date: date,
    to_date: date,
) -> list[MaterialisedObligation]:
    """Every obligation for this entity whose DUE DATE falls in the
    window. Pure: no I/O, no clock read, no LLM. Idempotency is the
    caller's job (ON CONFLICT DO NOTHING on the unique instance key)."""
    out: list[MaterialisedObligation] = []

    for rule in rules:
        verdict = applies_to(rule.get("applies_when") or {}, business)
        if verdict is False:
            continue
        status = "needs_entity_data" if verdict == UNKNOWN else "upcoming"

        cadence = rule.get("cadence")
        due_rule = rule.get("due_rule") or {}
        owner = rule.get("owner", "kunal")

        anchors: list[tuple[date, str]] = []
        if cadence == "monthly":
            y, m = _shift_month(from_date.year, from_date.month, -14)
            for _ in range(30):
                anchor = _clamp_day(y, m, 31)
                anchors.append((anchor, f"{calendar.month_abbr[m]}-{y}"))
                y, m = _shift_month(y, m, 1)
        elif cadence == "quarterly":
            for fy_start in range(from_date.year - 2, to_date.year + 1):
                for qi, end_m in enumerate((6, 9, 12, 3), start=1):
                    ey = fy_start if end_m != 3 else fy_start + 1
                    anchor = _clamp_day(ey, end_m, 31)
                    _, _, lbl = _fy_bounds(fy_start)
                    anchors.append((anchor, f"Q{qi}-{lbl}"))
        elif cadence == "annual":
            for fy_start in range(from_date.year - 3, to_date.year + 1):
                _, fy_end, lbl = _fy_bounds(fy_start)
                anchors.append((fy_end, lbl))
        else:
            continue  # 'event' — catalogued, never auto-materialised

        for anchor, label in anchors:
            due = due_date_for(due_rule, period_anchor=anchor)
            if due is None or not (from_date <= due <= to_date):
                continue
            first_alert, note = _alert_plan(due)
            out.append(MaterialisedObligation(
                rule_code=rule["code"],
                period_label=label,
                due_date=due,
                status=status,
                first_alert_on=first_alert,
                alert_note=note,
                owner=owner,
            ))

    out.sort(key=lambda o: (o.due_date, o.rule_code))
    return out


def exposure_for(rule: dict, *, due: date, today: date) -> Decimal:
    """penalty_per_day x days late. Arithmetic, never a model."""
    ppd = rule.get("penalty_per_day")
    if ppd is None or today <= due:
        return Decimal("0.00")
    days_late = (today - due).days
    return (Decimal(str(ppd)) * days_late).quantize(Decimal("0.01"))
