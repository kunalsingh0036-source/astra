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
from pydantic import BaseModel, Field

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


class CorpusIngestRequest(BaseModel):
    channel: str = Field(max_length=40, description="whatsapp_personal | instagram")
    format: str = Field(max_length=40, description="whatsapp_txt | instagram_json")
    self_name: str = Field(max_length=120)
    content: str = Field(min_length=1, max_length=10_000_000)
    contact: str = Field(default="", max_length=120)
    dry_run: bool = False


@router.post("/corpus")
async def ingest_corpus(req: CorpusIngestRequest) -> dict:
    """Ingest a WhatsApp .txt / Instagram JSON export into the voice
    corpus (keeps only Kunal's own messages; hash-deduped; dry_run
    parses without writing)."""
    from email_agent.services.voice_corpus import ingest_export

    return await ingest_export(
        channel=req.channel.strip(), fmt=req.format.strip(),
        content=req.content, self_name=req.self_name,
        contact=req.contact, dry_run=req.dry_run,
    )


@router.get("/corpus")
async def corpus_status() -> dict:
    from email_agent.services.voice_corpus import corpus_counts

    return {"ok": True, "counts": await corpus_counts()}


class DraftReplyRequest(BaseModel):
    channel: str = Field(default="whatsapp", max_length=40)
    their_message: str = Field(min_length=1, max_length=4000)
    contact: str = Field(default="", max_length=120)
    context: str = Field(default="", max_length=2000)
    instruction: str = Field(default="", max_length=400)


@router.post("/draft-reply")
async def draft_reply(req: DraftReplyRequest) -> dict:
    """Draft a copy-paste-ready personal reply in Kunal's mined texting
    voice. NEVER sends anything — he pastes it himself."""
    from email_agent.services.voice_reply import draft_personal_reply

    return await draft_personal_reply(
        channel=req.channel, their_message=req.their_message,
        contact=req.contact, context=req.context, instruction=req.instruction,
    )
