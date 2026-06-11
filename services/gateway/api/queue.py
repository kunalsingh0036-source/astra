"""POST /api/v1/queue/drain — send everything QUEUED and due.

Called by the cloud scheduler's wa_dispatch job every 60s with the
mesh secret. Replaces the celery-beat → worker dispatch path that was
never deployed (messages sat QUEUED forever). Protected by the
gateway's mesh-secret middleware like all /api/v1/* routes.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter

from gateway.services.dispatcher import drain_queue

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/queue", tags=["queue"])


@router.post("/drain")
async def drain() -> dict:
    return await drain_queue()
