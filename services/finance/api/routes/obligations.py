"""Statutory obligation endpoints — the deadline engine's HTTP surface.

The scan is deterministic and idempotent: running it twice creates zero
duplicates (unique on business+rule+period). Nothing here calls a model.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from finance.db.engine import get_session
from finance.db.ensure import ensure_ready
from finance.services.obligation_engine import exposure_for, materialise

router = APIRouter(prefix="/obligations", tags=["obligations"])

_IST_OFFSET = timedelta(hours=5, minutes=30)


def _today_ist() -> date:
    from datetime import datetime, timezone
    return (datetime.now(timezone.utc) + _IST_OFFSET).date()


async def _load_rules(session: AsyncSession) -> list[dict]:
    rows = (await session.execute(text("""
        SELECT code, label, authority, cadence, due_rule, applies_when,
               penalty_per_day, penalty_capped, penalty_note,
               statute_ref, source_url, verified_on, verified_by, status, owner
        FROM obligation_rules
    """))).mappings().all()
    return [dict(r) for r in rows]


async def _load_businesses(session: AsyncSession, business_id=None) -> list[dict]:
    sql = """
        SELECT id, name, slug, business_type, gstin, pan,
               gst_registration_type, employee_count, aato_last_fy,
               has_international_transactions, fy_end, lut_valid_till,
               paid_up_capital, is_active
        FROM businesses
        WHERE COALESCE(is_active, TRUE) = TRUE
    """
    params: dict = {}
    if business_id:
        sql += " AND id = :bid"
        params["bid"] = business_id
    rows = (await session.execute(text(sql), params)).mappings().all()
    return [dict(r) for r in rows]


@router.post("/scan")
async def scan(
    horizon_days: int = Query(180, ge=1, le=730),
    lookback_days: int = Query(120, ge=0, le=730),
    business_id: uuid.UUID | None = None,
    session: AsyncSession = Depends(get_session),
):
    """Materialise every obligation due in the window, for every active
    entity. Idempotent — safe to run on a schedule."""
    await ensure_ready()
    today = _today_ist()
    from_date = today - timedelta(days=lookback_days)
    to_date = today + timedelta(days=horizon_days)

    rules = await _load_rules(session)
    if not rules:
        return {"ok": False, "reason": "no obligation_rules seeded", "created": 0}
    businesses = await _load_businesses(session, business_id)
    if not businesses:
        return {"ok": False, "reason": "no active businesses — seed the entity master", "created": 0}

    by_code = {r["code"]: r for r in rules}
    created = 0
    for biz in businesses:
        planned = materialise(
            business=biz, rules=rules, from_date=from_date, to_date=to_date
        )
        for item in planned:
            rule = by_code[item.rule_code]
            res = await session.execute(text("""
                INSERT INTO obligations
                    (id, business_id, rule_code, period_label, due_date, status,
                     owner, exposure_amount, first_alert_on, alert_note)
                VALUES
                    (:id, :bid, :code, :period, :due, :status,
                     :owner, :exposure, :alert_on, :note)
                ON CONFLICT (business_id, rule_code, period_label) DO NOTHING
                RETURNING id
            """), {
                "id": uuid.uuid4(), "bid": biz["id"], "code": item.rule_code,
                "period": item.period_label, "due": item.due_date,
                "status": item.status, "owner": item.owner,
                "exposure": exposure_for(rule, due=item.due_date, today=today),
                "alert_on": item.first_alert_on, "note": item.alert_note,
            })
            if res.scalar() is not None:
                created += 1

    # Refresh live state on everything still open. Arithmetic only.
    await session.execute(text("""
        UPDATE obligations o
        SET status = CASE
                WHEN o.status IN ('filed','not_applicable') THEN o.status
                WHEN o.status = 'needs_entity_data' THEN o.status
                WHEN o.due_date < :today THEN 'overdue'
                WHEN o.due_date <= :soon THEN 'due'
                ELSE 'upcoming' END,
            exposure_amount = CASE
                WHEN o.status IN ('filed','not_applicable') THEN o.exposure_amount
                -- Applicability unknown => no rupee claim. Resolve the
                -- entity master first; a penalty figure for something that
                -- may not apply reads as fact and is not one.
                WHEN o.status = 'needs_entity_data' THEN 0
                WHEN o.due_date < :today THEN
                    COALESCE(r.penalty_per_day, 0) * (CAST(:today AS DATE) - o.due_date)
                ELSE 0 END,
            updated_at = now()
        FROM obligation_rules r
        WHERE r.code = o.rule_code
    """), {"today": today, "soon": today + timedelta(days=7)})
    await session.commit()

    return {"ok": True, "created": created,
            "window": {"from": str(from_date), "to": str(to_date)},
            "entities": len(businesses), "rules": len(rules)}


@router.get("/")
async def list_obligations(
    business_id: uuid.UUID | None = None,
    status: str | None = None,
    horizon_days: int = Query(120, ge=1, le=730),
    alertable_only: bool = False,
    session: AsyncSession = Depends(get_session),
):
    """Open obligations. `alertable_only` applies the source gate: a rule
    that is not 'verified' NEVER surfaces as an alert, only in the
    sign-off tray."""
    await ensure_ready()
    today = _today_ist()
    sql = """
        SELECT o.id, o.business_id, b.name AS business, b.slug,
               o.rule_code, r.label, r.authority, o.period_label,
               o.due_date, o.status, o.owner, o.exposure_amount,
               o.first_alert_on, o.alert_note,
               r.penalty_per_day, r.penalty_capped, r.penalty_note,
               r.statute_ref, r.source_url, r.status AS rule_status
        FROM obligations o
        JOIN obligation_rules r ON r.code = o.rule_code
        JOIN businesses b ON b.id = o.business_id
        WHERE o.due_date <= :horizon
    """
    params: dict = {"horizon": today + timedelta(days=horizon_days)}
    if business_id:
        sql += " AND o.business_id = :bid"
        params["bid"] = business_id
    if status:
        sql += " AND o.status = :st"
        params["st"] = status
    else:
        sql += " AND o.status NOT IN ('filed','not_applicable')"
    if alertable_only:
        sql += " AND r.status = 'verified' AND o.status <> 'needs_entity_data'"
        sql += " AND o.first_alert_on <= :today"
        params["today"] = today
    sql += " ORDER BY o.due_date ASC, r.penalty_capped ASC"

    rows = (await session.execute(text(sql), params)).mappings().all()
    return {"ok": True, "today": str(today), "count": len(rows),
            "obligations": [dict(r) for r in rows]}


@router.post("/{obligation_id}/file")
async def mark_filed(
    obligation_id: uuid.UUID,
    payload: dict,
    session: AsyncSession = Depends(get_session),
):
    """Record a filing. `filed_ref` (ARN/SRN/acknowledgement) is required —
    a filing with no reference is a claim, not a record."""
    await ensure_ready()
    ref = str((payload or {}).get("filed_ref") or "").strip()
    if not ref:
        raise HTTPException(400, "filed_ref required (ARN / SRN / acknowledgement no.)")
    raw_on = (payload or {}).get("filed_on")
    try:
        filed_on = date.fromisoformat(str(raw_on)) if raw_on else _today_ist()
    except ValueError:
        raise HTTPException(400, "filed_on must be YYYY-MM-DD")
    res = await session.execute(text("""
        UPDATE obligations
        SET status='filed', filed_on=:on, filed_ref=:ref,
            exposure_amount=0, updated_at=now()
        WHERE id=:id RETURNING id
    """), {"id": obligation_id, "on": filed_on, "ref": ref})
    if res.scalar() is None:
        raise HTTPException(404, f"no obligation {obligation_id}")
    await session.commit()
    return {"ok": True, "id": str(obligation_id), "status": "filed", "filed_ref": ref}


@router.get("/rules")
async def list_rules(session: AsyncSession = Depends(get_session)):
    """The catalogue, with the sign-off tray called out explicitly."""
    await ensure_ready()
    rows = (await session.execute(text("""
        SELECT code, label, authority, cadence, penalty_per_day, penalty_capped,
               penalty_note, statute_ref, source_url, verified_on, verified_by,
               status, owner
        FROM obligation_rules ORDER BY authority, code
    """))).mappings().all()
    rules = [dict(r) for r in rows]
    return {
        "ok": True, "count": len(rules),
        "verified": sum(1 for r in rules if r["status"] == "verified"),
        "needs_signoff": sum(1 for r in rules if r["status"] != "verified"),
        "rules": rules,
    }


@router.post("/rules/{code}/verify")
async def verify_rule(
    code: str, payload: dict | None = None,
    session: AsyncSession = Depends(get_session),
):
    """Sign a rule off. Only after this does it alert."""
    await ensure_ready()
    by = str((payload or {}).get("verified_by") or "kunal").strip()
    if by not in {"kunal", "ca", "cs"}:
        raise HTTPException(400, "verified_by must be one of: kunal, ca, cs")
    res = await session.execute(text("""
        UPDATE obligation_rules
        SET status='verified', verified_by=:by, verified_on=:on
        WHERE code=:code RETURNING code
    """), {"code": code, "by": by, "on": _today_ist()})
    if res.scalar() is None:
        raise HTTPException(404, f"no rule {code}")
    await session.commit()
    return {"ok": True, "code": code, "status": "verified", "verified_by": by}
