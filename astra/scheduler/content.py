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
        await _nudge_content_ready(
            result.get("title", ""), result.get("artifact_id")
        )
    return result


async def _nudge_content_ready(title: str, artifact_id: int | None = None) -> None:
    """Deliver the LinkedIn draft INTO WhatsApp — the post text itself,
    not a link to a queue. The Jul-02 audit: 11 drafts stuck in
    pending_review, 0 ever posted, because review lived on a web page
    Kunal never opens. He approves/refines/discards by replying here;
    the text is right in the thread to copy into LinkedIn.
    Best-effort; a failed nudge never fails the job."""
    post_text = ""
    if artifact_id is not None:
        try:
            from astra.creators.store import get_artifact

            art = await get_artifact(int(artifact_id))
            content = (art or {}).get("content") or {}
            hook = str(content.get("hook") or "").strip()
            body = str(content.get("body") or "").strip()
            post_text = f"{hook}\n\n{body}".strip() if (hook or body) else ""
            if len(post_text) > 1600:
                post_text = post_text[:1600].rstrip() + "…"
        except Exception as e:
            logger.info("[scheduler] content body fetch skipped: %s", e)

    if post_text:
        msg = (
            f"📝 Today's LinkedIn draft — “{title}”\n\n"
            f"{post_text}\n\n"
            "Reply “approve” to lock it (then copy-paste to LinkedIn), "
            "“refine: <what to change>”, or “discard”."
        )
    else:
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
