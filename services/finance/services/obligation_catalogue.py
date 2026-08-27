"""The seed catalogue of statutory obligations (India, FY2025-26 / 2026-27).

EVERY entry carries statute_ref + source_url + verified_on + verified_by,
and ships as status='unverified' — meaning it materialises into the
calendar but NEVER fires an alert until a human (ideally Kunal's CA)
signs it off. That one property is what makes a hallucinated deadline
structurally impossible rather than merely discouraged.

Reviewing this list is the single highest-leverage 45 minutes available:
it converts the entire deadline engine from "plausible" to "verified".

NOT catalogued deliberately: event-driven forms (FC-GPR, PAS-3, MGT-14,
ADT-1) fire off a transaction date the entity master does not hold, so
auto-materialising them would invent deadlines. They belong in a
transaction-triggered pass, not the calendar.

NOTE ON THE INCOME-TAX ACT 2025: section NUMBERS change for FY2026-27
onward (the 1961 Act is replaced from 1 Apr 2026). statute_ref values
below cite the sections in force for the periods they govern and must
be re-mapped when the new numbering binds.
"""

from datetime import date

_V = date(2026, 8, 24)   # verified_on for this seed batch
_BY = "seed"             # NOT 'ca' — nothing here is signed off yet

CATALOGUE: list[dict] = [
    # ── GST ────────────────────────────────────────────────────────
    {
        "code": "GSTR1_MONTHLY", "penalty_cap_amount": 5000, "label": "GSTR-1 (outward supplies)",
        "authority": "GST", "cadence": "monthly",
        "due_rule": {"kind": "monthly_day", "day": 11, "offset_months": 1},
        "applies_when": {"gst_registration_type": ["regular"]},
        "penalty_per_day": 50, "penalty_capped": True,
        "penalty_note": "Rs 50/day (Rs 20 nil), capped Rs 5,000 per return",
        "statute_ref": "Sec 37, CGST Act 2017",
        "source_url": "https://www.indiafilings.com/gst-return-filing/due-dates",
    },
    {
        "code": "GSTR3B_MONTHLY", "penalty_cap_amount": 5000, "label": "GSTR-3B + tax payment",
        "authority": "GST", "cadence": "monthly",
        "due_rule": {"kind": "monthly_day", "day": 20, "offset_months": 1},
        "applies_when": {"gst_registration_type": ["regular"]},
        "penalty_per_day": 50, "penalty_capped": True,
        "penalty_note": "Rs 50/day capped Rs 5,000, PLUS interest 18% p.a. on net "
                        "liability and 24% on excess ITC. Table 3 is hard-locked "
                        "since Jul-2025: errors must route via GSTR-1A/IMS.",
        "statute_ref": "Sec 39, CGST Act 2017",
        "source_url": "https://cleartax.in/s/gst-return-late-fees",
    },
    {
        "code": "GSTR9_ANNUAL", "label": "GSTR-9 annual return",
        "authority": "GST", "cadence": "annual",
        "due_rule": {"kind": "annual_mmdd", "mm": 12, "dd": 31},
        "applies_when": {"gst_registration_type": ["regular"], "aato_last_fy_gt": 20000000},
        "penalty_per_day": 200, "penalty_capped": True,
        "penalty_note": "Rs 200/day, capped at 0.25% of turnover",
        "statute_ref": "Sec 44, CGST Act 2017",
        "source_url": "https://cleartax.in/s/gstr-9-annual-return",
    },
    {
        "code": "LUT_RENEWAL", "label": "LUT (RFD-11) renewal for next FY",
        "authority": "GST", "cadence": "annual",
        "due_rule": {"kind": "annual_mmdd", "mm": 3, "dd": 31},
        "applies_when": {"gst_registration_type": ["regular"]},
        "penalty_per_day": None, "penalty_capped": True,
        "penalty_note": "No direct penalty, but every export becomes taxable at "
                        "18% with a refund fight until renewed. File in advance.",
        "statute_ref": "Rule 96A, CGST Rules 2017",
        "source_url": "https://taxgarden.in/blog/gst-refund-exporters-lut-bond-rfd-01-guide",
    },
    # ── TDS / Income tax ───────────────────────────────────────────
    {
        "code": "TDS_DEPOSIT", "label": "TDS deposit (challan)",
        "authority": "IT", "cadence": "monthly",
        "due_rule": {"kind": "monthly_day", "day": 7, "offset_months": 1},
        "applies_when": {},
        "penalty_per_day": None, "penalty_capped": True,
        "penalty_note": "Interest 1%/mo (deducted-not-deposited 1.5%/mo); 30% of "
                        "the expense disallowed u/s 40(a)(ia); Sec 276B prosecution "
                        "possible. March deductions are due 30 Apr.",
        "statute_ref": "Sec 200, Income-tax Act 1961",
        "source_url": "https://www.indiafilings.com/learn/tds-payment-due-dates-and-penalties",
    },
    {
        "code": "TDS_RETURN_Q", "label": "TDS return (24Q/26Q/27Q)",
        "authority": "IT", "cadence": "quarterly",
        "due_rule": {"kind": "quarterly_day", "day": 31, "offset_months": 1},
        "applies_when": {},
        "penalty_per_day": 200, "penalty_capped": True,
        "penalty_note": "Sec 234E Rs 200/day capped at the TDS amount, PLUS Sec 271H "
                        "Rs 10,000-1,00,000. Sec 271H relief now needs filing within "
                        "1 MONTH of due date (was 1 year).",
        "statute_ref": "Sec 200(3) r/w Rule 31A",
        "source_url": "https://taxgarden.in/blog/section-234e-271h-penalty-late-tds-return-filing-india",
    },
    {
        "code": "ITR_COMPANY", "label": "Income tax return (company / audit case)",
        "authority": "IT", "cadence": "annual",
        "due_rule": {"kind": "annual_mmdd", "mm": 10, "dd": 31},
        "applies_when": {"business_type": ["pvt_ltd", "llp"]},
        "penalty_per_day": None, "penalty_capped": True,
        "penalty_note": "THE EXPENSIVE ONE: a late ITR FORFEITS carry-forward of "
                        "business losses (Sec 80). For a capital-burning company that "
                        "dwarfs the Sec 234F fee and 234A interest.",
        "statute_ref": "Sec 139(1), Income-tax Act",
        "source_url": "https://taxguru.in/income-tax/itr-filing-due-dates-fy-2025-26-ay-2026-27.html",
    },
    {
        "code": "TAX_AUDIT_3CD", "label": "Tax audit report (Form 3CA/3CB + 3CD)",
        "authority": "IT", "cadence": "annual",
        "due_rule": {"kind": "annual_mmdd", "mm": 9, "dd": 30},
        "applies_when": {"aato_last_fy_gt": 10000000},
        "penalty_per_day": None, "penalty_capped": True,
        "penalty_note": "Sec 271B: 0.5% of turnover, max Rs 1,50,000. REQUIRES a "
                        "practising CA with UDIN — cannot be self-filed.",
        "statute_ref": "Sec 44AB, Income-tax Act",
        "source_url": "https://cleartax.in/s/tax-audit-section-44ab",
        "owner": "ca",
    },
    {
        "code": "FORM_3CEB", "label": "Form 3CEB (transfer pricing)",
        "authority": "IT", "cadence": "annual",
        "due_rule": {"kind": "annual_mmdd", "mm": 10, "dd": 31},
        "applies_when": {"has_international_transactions_is_true": True},
        "penalty_per_day": None, "penalty_capped": True,
        "penalty_note": "NO MONETARY THRESHOLD — any international transaction at "
                        "any value triggers it. Sec 271BA penalty is a FLAT "
                        "Rs 1,00,000. Requires a CA with UDIN.",
        "statute_ref": "Sec 92E, Income-tax Act",
        "source_url": "https://cleartax.in/s/how-to-file-form-3ceb",
        "owner": "ca",
    },
    # ── MCA / ROC — the uncapped ones ──────────────────────────────
    {
        "code": "AOC4", "label": "AOC-4 (financial statements)",
        "authority": "MCA", "cadence": "annual",
        "due_rule": {"kind": "annual_mmdd", "mm": 10, "dd": 30},
        "applies_when": {"business_type": ["pvt_ltd"]},
        "penalty_per_day": 100, "penalty_capped": False,
        "penalty_note": "Rs 100/day with NO CAP on the additional fee. Sec 137 adds "
                        "Rs 10,000 + Rs 100/day (company cap Rs 2L, officers Rs 50k). "
                        "Due 30 days from AGM; 30 Oct assumes a 30 Sep AGM.",
        "statute_ref": "Sec 137, Companies Act 2013",
        "source_url": "https://vakilsearch.com/article/roc-annual-filing-calendar-aoc4-mgt7-dpt3-india-2026/",
    },
    {
        "code": "MGT7", "label": "MGT-7 / MGT-7A (annual return)",
        "authority": "MCA", "cadence": "annual",
        "due_rule": {"kind": "annual_mmdd", "mm": 11, "dd": 29},
        "applies_when": {"business_type": ["pvt_ltd"]},
        "penalty_per_day": 100, "penalty_capped": False,
        "penalty_note": "Rs 100/day with NO CAP. Sec 92(5): Rs 10,000 + Rs 100/day, "
                        "company cap Rs 2L / officer Rs 50k. Due 60 days from AGM. "
                        "MGT-8 certification only above Rs 10cr paid-up / Rs 50cr turnover.",
        "statute_ref": "Sec 92, Companies Act 2013",
        "source_url": "https://clearlycomply.org/blog/roc-annual-filing-private-limited-company/",
    },
    {
        "code": "AGM", "label": "Annual General Meeting",
        "authority": "MCA", "cadence": "annual",
        "due_rule": {"kind": "annual_mmdd", "mm": 9, "dd": 30},
        "applies_when": {"business_type": ["pvt_ltd"]},
        "penalty_per_day": 5000, "penalty_capped": True,
        "penalty_note": "Sec 99: up to Rs 1,00,000 + Rs 5,000/day continuing. "
                        "AOC-4 and MGT-7 both hang off the AGM date.",
        "statute_ref": "Sec 96, Companies Act 2013",
        "source_url": "https://clearlycomply.org/blog/roc-annual-filing-private-limited-company/",
    },
    {
        "code": "DPT3", "label": "DPT-3 (return of deposits / non-deposit receipts)",
        "authority": "MCA", "cadence": "annual",
        "due_rule": {"kind": "annual_mmdd", "mm": 6, "dd": 30},
        "applies_when": {"business_type": ["pvt_ltd"]},
        "penalty_per_day": 500, "penalty_capped": True,
        "penalty_note": "Up to Rs 5,000 + Rs 500/day. Sec 73 exposure is severe if "
                        "the receipts are actually deposits — founder loans matter here.",
        "statute_ref": "Rule 16, Companies (Acceptance of Deposits) Rules 2014",
        "source_url": "https://www.caclubindia.com/articles/mca-compliance-relief-2026-dpt3-extension-ccfs-deadline-complete-roc-filing-calendar-55806.asp",
    },
    {
        "code": "MSME1_H1", "label": "Form MSME-1 (Apr-Sep half)",
        "authority": "MCA", "cadence": "annual",
        "due_rule": {"kind": "annual_mmdd", "mm": 10, "dd": 31},
        "applies_when": {"business_type": ["pvt_ltd"]},
        "penalty_per_day": None, "penalty_capped": True,
        "penalty_note": "Up to Rs 20,000 on the company + daily fine on directors. "
                        "Tracks payments to MSME vendors beyond 45 days (Sec 43B(h)).",
        "statute_ref": "Sec 405, Companies Act 2013",
        "source_url": "https://vakilsearch.com/article/roc-annual-filing-calendar-aoc4-mgt7-dpt3-india-2026/",
    },
    {
        "code": "MSME1_H2", "label": "Form MSME-1 (Oct-Mar half)",
        "authority": "MCA", "cadence": "annual",
        "due_rule": {"kind": "annual_mmdd", "mm": 4, "dd": 30},
        "applies_when": {"business_type": ["pvt_ltd"]},
        "penalty_per_day": None, "penalty_capped": True,
        "penalty_note": "Up to Rs 20,000 + daily fine on directors.",
        "statute_ref": "Sec 405, Companies Act 2013",
        "source_url": "https://vakilsearch.com/article/roc-annual-filing-calendar-aoc4-mgt7-dpt3-india-2026/",
    },
    {
        "code": "DIR3_KYC", "label": "DIR-3 KYC (director KYC)",
        "authority": "MCA", "cadence": "annual",
        "due_rule": {"kind": "annual_mmdd", "mm": 6, "dd": 30},
        "applies_when": {"business_type": ["pvt_ltd"]},
        "penalty_per_day": None, "penalty_capped": True,
        "penalty_note": "DIN DEACTIVATED + Rs 5,000 reactivation fee. Now TRIENNIAL "
                        "(amended Dec 2025) and moved to 30 June — stale checklists "
                        "still say annual/30 Sep.",
        "statute_ref": "Rule 12A, Companies (Appointment and Qualification of Directors) Rules",
        "source_url": "https://ebizfiling.com/blog/mca-revises-dir-3-kyc-norms/",
    },
    # ── Payroll ────────────────────────────────────────────────────
    {
        "code": "PF_ECR", "label": "PF (ECR filing + payment)",
        "authority": "EPFO", "cadence": "monthly",
        "due_rule": {"kind": "monthly_day", "day": 15, "offset_months": 1},
        "applies_when": {"employee_count_gte": 20},
        "penalty_per_day": None, "penalty_capped": True,
        "penalty_note": "12% p.a. interest PLUS damages of 5/10/15/25% by delay "
                        "bucket. PF damages are not waivable in practice.",
        "statute_ref": "Sec 6, EPF & MP Act 1952",
        "source_url": "https://taxgarden.in/blog/pf-esi-compliance-employer-contribution-rates-2026",
    },
    {
        "code": "ESI_CONTRIB", "label": "ESI contribution",
        "authority": "ESIC", "cadence": "monthly",
        "due_rule": {"kind": "monthly_day", "day": 15, "offset_months": 1},
        "applies_when": {"employee_count_gte": 10},
        "penalty_per_day": None, "penalty_capped": True,
        "penalty_note": "12% p.a. from the 16th. NOTE: ESI applies at 10+ employees "
                        "(wage ceiling Rs 21,000); registration is due within 15 days "
                        "of crossing and liability is RETROACTIVE.",
        "statute_ref": "Sec 39, ESI Act 1948",
        "source_url": "https://www.indiafilings.com/learn/pf-and-esi-payment",
    },
    {
        "code": "FORM16", "label": "Form 16 issuance to employees",
        "authority": "IT", "cadence": "annual",
        "due_rule": {"kind": "annual_mmdd", "mm": 6, "dd": 15},
        "applies_when": {"employee_count_gte": 1},
        "penalty_per_day": 100, "penalty_capped": True,
        "penalty_note": "Rs 100/day per certificate u/s 272A(2)(g).",
        "statute_ref": "Rule 31, Income-tax Rules",
        "source_url": "https://www.indiafilings.com/learn/tds-payment-due-dates-and-penalties",
    },
    # ── Audit (the unavoidable one) ────────────────────────────────
    {
        "code": "STATUTORY_AUDIT", "label": "Statutory audit by a practising CA",
        "authority": "MCA", "cadence": "annual",
        "due_rule": {"kind": "annual_mmdd", "mm": 9, "dd": 30},
        "applies_when": {"business_type": ["pvt_ltd"]},
        "penalty_per_day": None, "penalty_capped": True,
        "penalty_note": "NO THRESHOLD — every company, every year, even with zero "
                        "transactions. Non-compliance: company Rs 25,000-5,00,000, "
                        "every officer in default Rs 10,000-1,00,000. Cannot be "
                        "automated or self-signed: needs a CA's UDIN.",
        "statute_ref": "Sec 139 r/w Sec 143, Companies Act 2013",
        "source_url": "https://razorpay.com/rize/blogs/audit-requirement-for-private-limited-company",
        "owner": "ca",
    },
    # ── FEMA ───────────────────────────────────────────────────────
    {
        "code": "FLA_RETURN", "label": "FLA return to RBI (foreign assets & liabilities)",
        "authority": "RBI", "cadence": "annual",
        "due_rule": {"kind": "annual_mmdd", "mm": 7, "dd": 15},
        "applies_when": {"has_international_transactions_is_true": True},
        "penalty_per_day": None, "penalty_capped": True,
        "penalty_note": "FEMA compounding. Becomes due every year once foreign "
                        "investment sits on the books — i.e. from the seed onward.",
        "statute_ref": "FEMA 1999 r/w FLA Return directions",
        "source_url": "https://www.bhavyasharmaandassociates.com/fema-compliance-indian-startups-fc-gpr-fla-guide/",
    },
]


def seed_rows() -> list[dict]:
    """Catalogue -> DB rows. Ships UNVERIFIED: materialises, never alerts."""
    out = []
    for r in CATALOGUE:
        out.append({
            **r,
            "penalty_note": r.get("penalty_note", ""),
            "penalty_cap_amount": r.get("penalty_cap_amount"),
            "owner": r.get("owner", "kunal"),
            "verified_on": _V,
            "verified_by": _BY,
            "status": "unverified",
        })
    return out
