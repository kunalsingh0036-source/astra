"""
Daily LinkedIn content draft job — beachhead 2.

After the morning Research Intel briefing lands (07:00 IST), draft a
LinkedIn post from its OUTWARD insight and stage it for Kunal to
review → approve → post manually. Mirrors the inbox-triage rhythm:
stage silently, then send ONE nudge so the loop actually closes. A
staged draft nobody knows about is a post nobody ships.

Astra never posts to LinkedIn on Kunal's behalf — it drafts; he posts.
"""

from __future__ import annotations

import logging
import os

import httpx

from astra.creators.draft_linkedin_post import draft_linkedin_post

logger = logging.getLogger(__name__)


async def content_draft() -> dict:
    """Draft a LinkedIn post from the latest ready research briefing and
    nudge Kunal if one was staged. Idempotent (skips a briefing already
    drafted) and gated (skips meta-reviews / thin / non-postable)."""
    result = await draft_linkedin_post()
    logger.info("[scheduler] content_draft: %s", result)
    if result.get("status") == "staged":
        await _nudge_content_ready(result.get("title", ""))
    return result


async def _nudge_content_ready(title: str) -> None:
    """Ping Kunal that a LinkedIn draft is waiting — WhatsApp + push →
    /content. Best-effort; a failed nudge never fails the job."""
    msg = (
        "📝 A LinkedIn post is drafted and waiting for you"
        + (f": “{title}”." if title else ".")
        + "\nSay “show my post” to review it here, or open Astra → Content."
    )

    try:
        base = os.environ.get(
            "GATEWAY_URL", "http://whatsapp.railway.internal:8080"
        ).rstrip("/")
        headers = {
            "x-astra-secret": os.environ.get("AGENT_SHARED_SECRET", "").strip()
        }
        async with httpx.AsyncClient(timeout=30.0) as c:
            await c.post(
                f"{base}/api/v1/notify/owner", json={"text": msg}, headers=headers
            )
    except Exception as e:
        logger.info("[scheduler] content nudge WA skipped: %s", e)

    try:
        from astra.notifications import notify

        notify(
            title="LinkedIn post drafted",
            body=(title or "Ready to review & post")[:120],
            url="/content",
            tag="content-ready",
            also_push=True,
        )
    except Exception as e:
        logger.info("[scheduler] content nudge push skipped: %s", e)
