"""POST /api/v1/sync — trigger a Gmail sync cycle.

Called by the cloud scheduler every few minutes (astra/email/client.py
trigger_sync). Protected by the mesh-secret middleware like every
/api/v1/* route. Replaces the celery-beat → worker path that was
never deployed to Railway.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from email_agent.db.engine import get_session
from email_agent.services.sync_service import run_sync

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sync", tags=["sync"])


@router.post("/")
async def trigger_sync(session: AsyncSession = Depends(get_session)) -> dict:
    result = await run_sync(session)
    logger.info("[sync] %s", result)
    return result
