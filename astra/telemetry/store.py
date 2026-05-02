"""
Persist and read usage events.

`record_usage` accepts a ResultMessage-shaped dict (or the dataclass
itself, duck-typed) so callers in different processes don't need to
import the Agent SDK types.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select

from astra.db.engine import async_session
from astra.telemetry.models import UsageEvent

logger = logging.getLogger(__name__)


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Safe attribute/key lookup across dataclasses and plain dicts."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _flatten_usage(model_usage: dict[str, Any] | None) -> dict[str, int]:
    """
    model_usage is keyed by model name:
      { "claude-sonnet-4-6": { "input_tokens": ..., "output_tokens": ...,
                               "cache_read_input_tokens": ...,
                               "cache_creation_input_tokens": ... } }
    Sum the token counts across models so we can store one row per turn.
    """
    totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
    }
    if not model_usage:
        return totals
    for _model, u in model_usage.items():
        if not isinstance(u, dict):
            continue
        totals["input_tokens"] += int(u.get("input_tokens") or 0)
        totals["output_tokens"] += int(u.get("output_tokens") or 0)
        totals["cache_read_tokens"] += int(u.get("cache_read_input_tokens") or 0)
        totals["cache_creation_tokens"] += int(u.get("cache_creation_input_tokens") or 0)
    return totals


async def record_usage(result: Any, *, source: str = "chat") -> None:
    """
    Write one UsageEvent for a ResultMessage. Swallows errors — cost
    tracking should never break the agent loop.
    """
    try:
        model_usage = _get(result, "model_usage") or {}
        models = ",".join(sorted(model_usage.keys())) if model_usage else None

        totals = _flatten_usage(model_usage)
        # Fall back to `usage` dict if model_usage was empty but a flat
        # `usage` snapshot came back.
        flat = _get(result, "usage") or {}
        if totals["input_tokens"] == 0 and isinstance(flat, dict):
            totals["input_tokens"] = int(flat.get("input_tokens") or 0)
            totals["output_tokens"] = int(flat.get("output_tokens") or 0)
            totals["cache_read_tokens"] = int(flat.get("cache_read_input_tokens") or 0)
            totals["cache_creation_tokens"] = int(
                flat.get("cache_creation_input_tokens") or 0
            )

        event = UsageEvent(
            session_id=_get(result, "session_id"),
            subtype=_get(result, "subtype"),
            stop_reason=_get(result, "stop_reason"),
            models=models,
            input_tokens=totals["input_tokens"],
            output_tokens=totals["output_tokens"],
            cache_read_tokens=totals["cache_read_tokens"],
            cache_creation_tokens=totals["cache_creation_tokens"],
            cost_usd=float(_get(result, "total_cost_usd") or 0.0),
            duration_ms=int(_get(result, "duration_ms") or 0),
            num_turns=int(_get(result, "num_turns") or 0),
            is_error=bool(_get(result, "is_error") or False),
            source=source,
        )

        async with async_session() as session:
            session.add(event)
            await session.commit()
    except Exception:
        logger.exception("failed to record usage event")


async def usage_summary(days: int = 30) -> dict[str, Any]:
    """
    Aggregate usage for the last N days. Returns totals plus daily
    breakdown suitable for a simple chart.
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)

    async with async_session() as session:
        totals = await session.execute(
            select(
                func.coalesce(func.sum(UsageEvent.cost_usd), 0.0),
                func.coalesce(func.sum(UsageEvent.input_tokens), 0),
                func.coalesce(func.sum(UsageEvent.output_tokens), 0),
                func.coalesce(func.sum(UsageEvent.cache_read_tokens), 0),
                func.coalesce(func.sum(UsageEvent.cache_creation_tokens), 0),
                func.count(UsageEvent.id),
            ).where(UsageEvent.ts >= since)
        )
        (
            cost,
            input_tok,
            output_tok,
            cache_read,
            cache_creation,
            turns,
        ) = totals.one()

        today_start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        today_cost_row = await session.execute(
            select(func.coalesce(func.sum(UsageEvent.cost_usd), 0.0)).where(
                UsageEvent.ts >= today_start
            )
        )
        today_cost = float(today_cost_row.scalar() or 0.0)

        daily_rows = await session.execute(
            select(
                func.date_trunc("day", UsageEvent.ts).label("day"),
                func.coalesce(func.sum(UsageEvent.cost_usd), 0.0),
                func.coalesce(func.sum(UsageEvent.input_tokens), 0),
                func.coalesce(func.sum(UsageEvent.output_tokens), 0),
                func.count(UsageEvent.id),
            )
            .where(UsageEvent.ts >= since)
            .group_by("day")
            .order_by("day")
        )
        daily = [
            {
                "day": row[0].isoformat() if row[0] else None,
                "cost_usd": float(row[1] or 0.0),
                "input_tokens": int(row[2] or 0),
                "output_tokens": int(row[3] or 0),
                "turns": int(row[4] or 0),
            }
            for row in daily_rows.all()
        ]

    return {
        "window_days": days,
        "total_cost_usd": float(cost or 0.0),
        "today_cost_usd": today_cost,
        "input_tokens": int(input_tok or 0),
        "output_tokens": int(output_tok or 0),
        "cache_read_tokens": int(cache_read or 0),
        "cache_creation_tokens": int(cache_creation or 0),
        "turns": int(turns or 0),
        "daily": daily,
    }
