"""Point-of-use schema guard for the finance service.

The finance service has NO migration runner in its deploy path (it is
mounted inside `agents`, whose entrypoint does not run alembic). So this
guard — called from main.py's lifespan — is what actually makes the
columns and tables exist in production.

Same pattern and same discipline as astra/creators/engagement.py:33 and
astra/research/spine.py: write the migration for the record, and keep
this SQL IDENTICAL to it. Every statement is IF NOT EXISTS, so it is a
safe no-op on an already-migrated database and a constructive create on
a fresh one.
"""

from __future__ import annotations

import logging

from sqlalchemy import text

from finance.db.engine import async_session

logger = logging.getLogger(__name__)

# ── Entity master: everything the obligation engine predicates on ──
# Each is a FACT KUNAL SUPPLIES. NULL stays NULL, and the engine reads
# NULL as "cannot assert" (status='needs_entity_data'), never as "does
# not apply" — see services/obligation_engine.applies_to().
_BUSINESS_COLUMNS = [
    ("cin", "VARCHAR(25)"),
    ("tan", "VARCHAR(15)"),
    ("state_code", "VARCHAR(4)"),
    ("registered_state", "VARCHAR(60)"),
    ("incorporation_date", "DATE"),
    ("fy_end", "VARCHAR(5) DEFAULT '03-31'"),
    ("gst_registration_type", "VARCHAR(20)"),   # regular|composition|unregistered
    ("gst_registered_on", "DATE"),
    ("lut_arn", "VARCHAR(40)"),
    ("lut_valid_till", "DATE"),
    ("pf_code", "VARCHAR(30)"),
    ("esic_code", "VARCHAR(30)"),
    ("paid_up_capital", "NUMERIC(16,2)"),
    ("aato_last_fy", "NUMERIC(16,2)"),
    ("has_international_transactions", "BOOLEAN"),  # the 3CEB tripwire
    ("employee_count", "INTEGER"),
    ("cash_low_threshold", "NUMERIC(14,2)"),
    ("is_active", "BOOLEAN DEFAULT TRUE"),
]

_RULE_COLUMNS = [
    # penalty_capped told us a cap EXISTS but not what it is, so exposure
    # kept accruing past the legal ceiling and overstated the liability.
    ("penalty_cap_amount", "NUMERIC(12,2)"),
]

_OBLIGATION_RULES = """
CREATE TABLE IF NOT EXISTS obligation_rules (
    code             VARCHAR(40) PRIMARY KEY,
    label            VARCHAR(200) NOT NULL,
    authority        VARCHAR(20)  NOT NULL,
    cadence          VARCHAR(20)  NOT NULL,
    due_rule         JSONB        NOT NULL,
    applies_when     JSONB        NOT NULL DEFAULT '{}'::jsonb,
    penalty_per_day  NUMERIC(12,2),
    penalty_capped   BOOLEAN      NOT NULL DEFAULT TRUE,
    penalty_note     TEXT         NOT NULL DEFAULT '',
    statute_ref      VARCHAR(200) NOT NULL,
    source_url       TEXT         NOT NULL,
    verified_on      DATE         NOT NULL,
    verified_by      VARCHAR(40)  NOT NULL,
    status           VARCHAR(20)  NOT NULL DEFAULT 'unverified',
    owner            VARCHAR(20)  NOT NULL DEFAULT 'kunal',
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT now()
)
"""

_OBLIGATIONS = """
CREATE TABLE IF NOT EXISTS obligations (
    id              UUID PRIMARY KEY,
    business_id     UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    rule_code       VARCHAR(40) NOT NULL REFERENCES obligation_rules(code) ON DELETE CASCADE,
    period_label    VARCHAR(30) NOT NULL,
    due_date        DATE NOT NULL,
    status          VARCHAR(24) NOT NULL DEFAULT 'upcoming',
    filed_on        DATE,
    filed_ref       VARCHAR(120),
    owner           VARCHAR(20) NOT NULL DEFAULT 'kunal',
    exposure_amount NUMERIC(14,2) NOT NULL DEFAULT 0,
    first_alert_on  DATE NOT NULL,
    alert_note      TEXT NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_obligation_instance UNIQUE (business_id, rule_code, period_label)
)
"""

_INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_obligations_due ON obligations (due_date, status)",
    "CREATE INDEX IF NOT EXISTS ix_obligations_business ON obligations (business_id, status)",
]


_ensured = False


async def ensure_ready() -> None:
    """Lazy, once-per-process guard — call at the top of any route that
    touches the obligation tables.

    WHY LAZY AND NOT lifespan: this app is MOUNTED (app.mount("/finance",
    ...)) inside `agents`, and Starlette does NOT run lifespan handlers
    for mounted sub-apps. Middleware runs, lifespan does not. Verified in
    production 2026-08-24: zero "[finance] ensure_schema" log lines ever
    appeared and the tables did not exist. Same shape as
    astra/creators/engagement.py, which calls its guard per-function for
    exactly this class of reason.
    """
    global _ensured
    if _ensured:
        return
    await ensure_schema()
    report = await seed_rules()
    logger.info("[finance] schema ready; rule catalogue: %s", report)
    _ensured = True


async def ensure_schema() -> None:
    """Idempotent. Logs loudly on failure; never silently half-applies."""
    async with async_session() as s:
        try:
            for col, ddl in _BUSINESS_COLUMNS:
                await s.execute(
                    text(f"ALTER TABLE businesses ADD COLUMN IF NOT EXISTS {col} {ddl}")
                )
            await s.execute(text(_OBLIGATION_RULES))
            for col, ddl in _RULE_COLUMNS:
                await s.execute(text(
                    f"ALTER TABLE obligation_rules ADD COLUMN IF NOT EXISTS {col} {ddl}"))
            await s.execute(text(_OBLIGATIONS))
            for idx in _INDEXES:
                await s.execute(text(idx))
            await s.commit()
            logger.info("[finance] ensure_schema OK")
        except Exception:
            await s.rollback()
            logger.exception("[finance] ensure_schema FAILED")
            raise


async def seed_rules(*, overwrite_unverified: bool = True) -> dict:
    """Upsert the catalogue.

    NEVER overwrites a rule a human has marked 'verified' — that sign-off
    is the whole point of the source gate, and a redeploy silently
    reverting it would quietly re-enable alerts on unreviewed rules.
    """
    from finance.services.obligation_catalogue import seed_rows

    inserted = updated = skipped_verified = 0
    async with async_session() as s:
        for row in seed_rows():
            existing = (await s.execute(
                text("SELECT status FROM obligation_rules WHERE code = :c"),
                {"c": row["code"]},
            )).scalar()
            if existing == "verified" and not overwrite_unverified:
                skipped_verified += 1
                continue
            if existing == "verified":
                skipped_verified += 1
                continue
            params = {
                "code": row["code"], "label": row["label"],
                "authority": row["authority"], "cadence": row["cadence"],
                "due_rule": __import__("json").dumps(row["due_rule"]),
                "applies_when": __import__("json").dumps(row.get("applies_when") or {}),
                "ppd": row.get("penalty_per_day"),
                "capped": bool(row.get("penalty_capped", True)),
                "note": row.get("penalty_note", ""),
                "statute": row["statute_ref"], "src": row["source_url"],
                "von": row["verified_on"], "vby": row["verified_by"],
                "cap": row.get("penalty_cap_amount"),
                "status": row["status"], "owner": row.get("owner", "kunal"),
            }
            res = await s.execute(text("""
                INSERT INTO obligation_rules
                    (code, label, authority, cadence, due_rule, applies_when,
                     penalty_per_day, penalty_capped, penalty_cap_amount,
                     penalty_note,
                     statute_ref, source_url, verified_on, verified_by, status, owner)
                VALUES
                    (:code, :label, :authority, :cadence, CAST(:due_rule AS JSONB),
                     CAST(:applies_when AS JSONB), :ppd, :capped, :cap, :note,
                     :statute, :src, :von, :vby, :status, :owner)
                ON CONFLICT (code) DO UPDATE SET
                    label = EXCLUDED.label, authority = EXCLUDED.authority,
                    cadence = EXCLUDED.cadence, due_rule = EXCLUDED.due_rule,
                    applies_when = EXCLUDED.applies_when,
                    penalty_per_day = EXCLUDED.penalty_per_day,
                    penalty_capped = EXCLUDED.penalty_capped,
                    penalty_cap_amount = EXCLUDED.penalty_cap_amount,
                    penalty_note = EXCLUDED.penalty_note,
                    statute_ref = EXCLUDED.statute_ref, source_url = EXCLUDED.source_url,
                    verified_on = EXCLUDED.verified_on, verified_by = EXCLUDED.verified_by,
                    owner = EXCLUDED.owner
                RETURNING (xmax = 0) AS inserted
            """), params)
            if res.scalar():
                inserted += 1
            else:
                updated += 1
        await s.commit()
    return {"inserted": inserted, "updated": updated,
            "skipped_verified": skipped_verified}
