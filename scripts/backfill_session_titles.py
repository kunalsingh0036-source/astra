#!/usr/bin/env python3
"""
Backfill session titles for sessions that don't have one yet.

The agent's finalize hook now generates a title via Haiku for every
new session as its first turn completes. This script catches up the
historical sessions: every distinct session_id in `turns` that has
no row in `session_titles`.

Usage:
    python scripts/backfill_session_titles.py [--limit N] [--concurrency K]

Defaults: process all sessions, 4 concurrent Haiku calls. The
generator function is idempotent (skips if already titled), so
re-running the script is safe.

Cost: claude-haiku-4-5 at ~$0.80/M input + $4/M output. A typical
title call uses ~500 input tokens + ~30 output tokens, so each
title is ~$0.0005. Backfilling 100 sessions ≈ $0.05. The dominant
"cost" is wall time (~1s per Haiku call); --concurrency=4 makes
100 sessions ~25s.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import Sequence

from sqlalchemy import text

# Make the astra package importable when run from the repo root.
sys.path.insert(0, ".")
from astra.db.engine import async_session  # noqa: E402
from astra.runtime.session_title import (  # noqa: E402
    generate_and_store_title,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("backfill")


async def _untitled_sessions(limit: int | None) -> list[str]:
    """Return session_ids that are in turns but not in session_titles."""
    sql = """
        SELECT DISTINCT t.session_id
        FROM turns t
        LEFT JOIN session_titles st ON st.session_id = t.session_id
        WHERE t.session_id IS NOT NULL
          AND t.status = 'complete'
          AND st.session_id IS NULL
        ORDER BY t.session_id
    """
    if limit is not None:
        sql += f"\nLIMIT {int(limit)}"
    async with async_session() as s:
        r = await s.execute(text(sql))
        rows = r.all()
    return [row[0] for row in rows]


async def _process(session_id: str, sem: asyncio.Semaphore) -> None:
    async with sem:
        try:
            title = await generate_and_store_title(session_id)
            if title:
                logger.info("✓ %s · %s", session_id[:8], title)
            else:
                logger.warning("∅ %s · no title generated", session_id[:8])
        except Exception:
            logger.exception("✗ %s · raised", session_id[:8])


async def main(limit: int | None, concurrency: int) -> int:
    sessions = await _untitled_sessions(limit)
    if not sessions:
        logger.info("nothing to backfill — all sessions are titled.")
        return 0
    logger.info(
        "backfilling %d session(s) with concurrency=%d",
        len(sessions),
        concurrency,
    )
    sem = asyncio.Semaphore(max(1, concurrency))
    await asyncio.gather(*[_process(sid, sem) for sid in sessions])
    logger.info("done.")
    return 0


def cli(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="max sessions to process (default: all untitled)",
    )
    p.add_argument(
        "--concurrency",
        type=int,
        default=4,
        help="parallel Haiku calls (default 4)",
    )
    args = p.parse_args(argv)
    return asyncio.run(main(args.limit, args.concurrency))


if __name__ == "__main__":
    sys.exit(cli())
