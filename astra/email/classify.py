"""
Batch classifier sweep — walks every unclassified inbound message
and runs email-agent's /ai/classify endpoint on it.

Strategy:
  * Page through /api/v1/messages/ (inbound only) in chunks of 50.
  * Filter client-side to rows where `ai_category` is null OR
    `ai_summary` is the literal fallback string "Classification
    unavailable" (left behind by earlier misfires that we're
    explicitly retrying).
  * Throttle to N concurrent classify calls to respect rate limits.
  * Each classify call persists ai_* fields on the row (email-agent
    does the DB write), so this function is idempotent across runs.

Called by scheduler at 12:40 IST (5 min before inbox_preview) and
on-demand via the `email_classify_sweep` MCP tool.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


BASE_URL = "http://localhost:8005"
DEFAULT_CONCURRENCY = 3
DEFAULT_MAX_PER_RUN = 50
FALLBACK_SUMMARY = "Classification unavailable"


async def classify_sweep(
    *,
    max_messages: int = DEFAULT_MAX_PER_RUN,
    concurrency: int = DEFAULT_CONCURRENCY,
    include_retries: bool = True,
) -> dict[str, Any]:
    """Classify up to `max_messages` unclassified inbound rows.

    `include_retries=True` also re-runs rows that previously saved
    the 'Classification unavailable' fallback (our auth-bug artifacts).
    """
    pending = await _collect_targets(
        max_messages=max_messages,
        include_retries=include_retries,
    )
    if not pending:
        return {
            "status": "success",
            "scanned": 0,
            "classified": 0,
            "failed": 0,
            "skipped": 0,
        }

    sem = asyncio.Semaphore(concurrency)
    successes = 0
    failures = 0

    async with httpx.AsyncClient(timeout=30) as client:
        async def _one(msg_id: str) -> bool:
            async with sem:
                try:
                    r = await client.post(
                        f"{BASE_URL}/api/v1/ai/classify/{msg_id}",
                        json={},
                    )
                    if r.status_code != 200:
                        logger.warning(
                            "[classify] msg=%s HTTP %s",
                            msg_id, r.status_code,
                        )
                        return False
                    body = r.json()
                    # Treat persisted-fallback as a failure so the retry
                    # loop can pick it up next run.
                    return body.get("summary") != FALLBACK_SUMMARY
                except Exception as e:
                    logger.warning(
                        "[classify] msg=%s error: %s", msg_id, e,
                    )
                    return False

        results = await asyncio.gather(
            *(_one(m["id"]) for m in pending),
            return_exceptions=False,
        )

    successes = sum(1 for ok in results if ok)
    failures = len(results) - successes

    logger.info(
        "[classify] sweep done — scanned=%d classified=%d failed=%d",
        len(pending), successes, failures,
    )
    return {
        "status": "success",
        "scanned": len(pending),
        "classified": successes,
        "failed": failures,
    }


async def _collect_targets(
    *, max_messages: int, include_retries: bool,
) -> list[dict[str, Any]]:
    """Page through inbound messages to find ones needing classification.

    email-agent's list endpoint caps at 200 per request and has no
    "unclassified only" flag, so we paginate + filter client-side.
    Good enough for 100s of messages; if volume grows we swap to a
    DB-side query.
    """
    targets: list[dict[str, Any]] = []
    offset = 0
    page_size = 200
    scanned = 0
    MAX_SCAN = 600  # Upper bound to avoid walking 10000+ rows needlessly.

    async with httpx.AsyncClient(timeout=15) as client:
        while len(targets) < max_messages and scanned < MAX_SCAN:
            r = await client.get(
                f"{BASE_URL}/api/v1/messages/",
                params={
                    "direction": "inbound",
                    "limit": page_size,
                    "offset": offset,
                },
            )
            if r.status_code != 200:
                logger.warning(
                    "[classify] list HTTP %s at offset=%d",
                    r.status_code, offset,
                )
                break
            rows = r.json()
            if not rows:
                break
            scanned += len(rows)
            for m in rows:
                cat = m.get("ai_category")
                summary = m.get("ai_summary") or ""
                if cat is None:
                    targets.append({"id": m["id"]})
                elif include_retries and summary == FALLBACK_SUMMARY:
                    targets.append({"id": m["id"]})
                if len(targets) >= max_messages:
                    break
            if len(rows) < page_size:
                break
            offset += page_size

    return targets
