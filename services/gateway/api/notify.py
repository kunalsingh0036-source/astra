"""POST /api/v1/notify/owner — send a text straight to Kunal.

Used by the scheduler's briefings (and anything else mesh-internal
that needs to reach Kunal on WhatsApp). Deliberately NOT the
/api/v1/send queue path: that one exists for business agents and
enforces agent registration + cooldowns + queueing. Reaching the
owner is the same trust domain as the WhatsApp→Astra chat channel,
so it shares its allowlist: targets must be in ASTRA_OWNER_NUMBERS.
Fail-closed — allowlist empty means this endpoint sends nothing.

Free-form text needs an open 24h session window with the recipient
(Meta rule). Once Kunal texts Astra daily the window stays open
naturally; until then sends may be rejected by Meta and we report
that honestly in the response.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from pydantic import BaseModel, Field

from gateway.services.astra_chat import is_owner, owner_numbers

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/notify", tags=["notify"])


class OwnerNotifyRequest(BaseModel):
    text: str = Field(min_length=1, max_length=60_000)
    phone: str | None = Field(
        default=None,
        description="Target number; defaults to the first owner number. "
        "Must be in ASTRA_OWNER_NUMBERS either way.",
    )


@router.post("/owner")
async def notify_owner(req: OwnerNotifyRequest) -> dict:
    from gateway.services.astra_chat import _WA_TEXT_LIMIT, _chunks
    from gateway.services.meta_api import MetaAPIClient

    owners = sorted(owner_numbers())
    target = (req.phone or (owners[0] if owners else "")).lstrip("+")
    if not target or not is_owner(target):
        return {
            "ok": False,
            "error": "no owner number configured (ASTRA_OWNER_NUMBERS) "
            "or target not in allowlist",
        }

    client = MetaAPIClient()
    sent = 0
    error: str | None = None
    try:
        for chunk in _chunks(req.text, _WA_TEXT_LIMIT):
            result = await client.send_text(phone=target, body=chunk)
            if not result.success:
                error = result.error
                break
            sent += 1
    finally:
        await client.close()

    if error:
        logger.warning("[notify/owner] send failed after %d chunk(s): %s", sent, error)
        return {"ok": False, "sent_chunks": sent, "error": error}
    return {"ok": True, "sent_chunks": sent}
