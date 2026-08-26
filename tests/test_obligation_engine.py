"""Regression tests for the obligation engine.

This is the highest-consequence pure function in the finance stack: it
decides which Rs 100/day-uncapped filings exist. Every bug found in
production during the 2026-08-24 acceptance run is pinned here.
"""

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))

from finance.services.obligation_engine import (  # noqa: E402
    UNKNOWN, applies_to, exposure_for, materialise,
)

PVT = {"business_type": "PVT_LTD", "gst_registration_type": "regular",
       "employee_count": 13, "fy_end": "03-31"}

RULES = [
    {"code": "AOC4", "cadence": "annual", "owner": "kunal",
     "due_rule": {"kind": "annual_mmdd", "mm": 10, "dd": 30},
     "applies_when": {"business_type": ["pvt_ltd"]},
     "penalty_per_day": 100, "penalty_capped": False},
    {"code": "MGT7", "cadence": "annual", "owner": "kunal",
     "due_rule": {"kind": "annual_mmdd", "mm": 11, "dd": 29},
     "applies_when": {"business_type": ["pvt_ltd"]},
     "penalty_per_day": 100, "penalty_capped": False},
    {"code": "GSTR3B", "cadence": "monthly", "owner": "kunal",
     "due_rule": {"kind": "monthly_day", "day": 20, "offset_months": 1},
     "applies_when": {"gst_registration_type": ["regular"]},
     "penalty_per_day": 50, "penalty_capped": True},
]


def test_enum_name_from_db_still_matches_rule_value():
    """SQLAlchemy Enum persists the member NAME ('PVT_LTD') while the
    catalogue speaks the VALUE ('pvt_ltd'). A naive == returned a
    CONFIDENT False and silently dropped AOC-4, MGT-7, ITR and the
    statutory audit in production."""
    assert applies_to({"business_type": ["pvt_ltd"]}, {"business_type": "PVT_LTD"}) is True
    assert applies_to({"business_type": ["pvt_ltd"]}, {"business_type": "LLP"}) is False


def test_unknown_is_never_not_applicable():
    """Missing entity data must read 'I cannot tell', never 'does not
    apply' — the two have very different costs at Rs 100/day."""
    assert applies_to({"gst_registration_type": ["regular"]},
                      {"gst_registration_type": None}) == UNKNOWN
    assert applies_to({"employee_count_gte": 10}, {}) == UNKNOWN


def test_uncapped_filings_land_on_the_right_dates():
    got = materialise(business=PVT, rules=RULES,
                      from_date=date(2026, 8, 1), to_date=date(2026, 12, 31))
    by = {o.rule_code: o for o in got}
    assert by["AOC4"].due_date == date(2026, 10, 30)
    assert by["MGT7"].due_date == date(2026, 11, 29)
    assert by["AOC4"].period_label == "FY2025-26"


def test_unknown_entity_data_materialises_flagged_not_skipped():
    biz = {"business_type": "PVT_LTD"}          # gst type absent
    got = materialise(business=biz, rules=RULES,
                      from_date=date(2026, 8, 1), to_date=date(2026, 10, 1))
    gstr = [o for o in got if o.rule_code == "GSTR3B"]
    assert gstr, "GSTR3B must still materialise when applicability is unknown"
    assert all(o.status == "needs_entity_data" for o in gstr)


def test_nationals_blackout_pulls_the_alert_forward():
    got = materialise(business=PVT, rules=RULES,
                      from_date=date(2026, 11, 1), to_date=date(2026, 11, 30))
    nov = [o for o in got if o.due_date == date(2026, 11, 20)]
    assert nov, "expected the Nov GSTR3B instance"
    o = nov[0]
    assert (o.due_date - o.first_alert_on).days == 45
    assert "Nationals" in o.alert_note


def test_exposure_is_arithmetic():
    assert exposure_for({"penalty_per_day": 100},
                        due=date(2026, 10, 30), today=date(2026, 12, 29)) == Decimal("6000.00")
    assert exposure_for({"penalty_per_day": 100},
                        due=date(2026, 10, 30), today=date(2026, 9, 1)) == Decimal("0.00")
    assert exposure_for({"penalty_per_day": None},
                        due=date(2026, 1, 1), today=date(2026, 12, 1)) == Decimal("0.00")


def test_materialise_is_deterministic():
    a = materialise(business=PVT, rules=RULES,
                    from_date=date(2026, 8, 1), to_date=date(2027, 3, 31))
    b = materialise(business=PVT, rules=RULES,
                    from_date=date(2026, 8, 1), to_date=date(2027, 3, 31))
    assert a == b


def test_first_financial_year_sec_2_41():
    """Companies Act Sec 2(41): a company incorporated Jan-Mar has its
    first FY end on 31 Mar of the FOLLOWING year. Without this, an entity
    incorporated Feb-2026 gets FY2025-26 AOC-4/MGT-7/ITR obligations that
    legally do not exist — a fabricated Rs 100/day deadline. Caught when
    Kunal's real entity master landed."""
    from datetime import date as _d

    from finance.services.obligation_engine import first_fy_end

    assert first_fy_end(_d(2026, 2, 19)) == _d(2027, 3, 31)   # HelmTech
    assert first_fy_end(_d(2026, 2, 27)) == _d(2027, 3, 31)   # TAC Squash
    assert first_fy_end(_d(2025, 11, 5)) == _d(2026, 3, 31)
    assert first_fy_end(None) is None


def test_no_obligation_predates_the_entity():
    biz = {"business_type": "PVT_LTD", "incorporation_date": date(2026, 2, 19)}
    got = materialise(business=biz, rules=RULES,
                      from_date=date(2025, 1, 1), to_date=date(2028, 3, 31))
    aoc = [o for o in got if o.rule_code == "AOC4"]
    assert aoc, "AOC-4 must still exist for the FIRST real FY"
    assert all(o.period_label != "FY2025-26" for o in aoc)
    assert aoc[0].period_label == "FY2026-27"
    # monthly returns must not predate incorporation either
    monthly = [o for o in got if o.rule_code == "GSTR3B"]
    assert all(o.due_date >= date(2026, 2, 1) for o in monthly)
