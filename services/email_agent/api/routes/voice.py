"""Voice endpoints — the learn-Kunal's-voice pipeline surface.

POST /api/v1/voice/backfill-sent  — start the full in:sent history pull
GET  /api/v1/voice/backfill-sent  — progress of the running/last pull
POST /api/v1/voice/mine           — run the register miner over the corpus
GET  /api/v1/voice/profile        — a mined profile (LinkedIn drafter etc.)

All mesh-auth via the /api/v1 middleware.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Query

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/voice", tags=["voice"])


@router.post("/backfill-sent")
async def backfill_sent(max_messages: int = Query(2000, ge=50, le=10000)) -> dict:
    from email_agent.services.sent_backfill import start_backfill

    return start_backfill(max_messages=max_messages)


@router.get("/backfill-sent")
async def backfill_sent_status() -> dict:
    from email_agent.services.sent_backfill import backfill_status

    return backfill_status()


@router.post("/mine")
async def mine() -> dict:
    """Start mining in the background — returns immediately (mining is
    classify + up to 5 distills; way past any mesh client timeout).
    Poll GET /voice/mine for the result."""
    from email_agent.services.voice_miner import start_mine

    return start_mine()


@router.get("/mine")
async def mine_progress() -> dict:
    from email_agent.services.voice_miner import mine_status

    return {"ok": True, **mine_status()}


@router.get("/profile")
async def profile(register: str = Query("general", max_length=40)) -> dict:
    from email_agent.services.voice_miner import get_profile

    return await get_profile(register)
