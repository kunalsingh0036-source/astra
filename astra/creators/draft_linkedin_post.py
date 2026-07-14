"""
Draft a LinkedIn post from a research briefing — beachhead 2.

The daily Research Intel briefing is Kunal's PRIVATE strategy document:
its `## Build / ## Subtract / ## Urgent / ## Action items` sections are
his internal roadmap. A public post must never touch those. So before
the model sees anything, `_extract_outward()` strips the briefing down
to its outward-facing market insight only (Gist + Findings + Signals +
Sources). The model physically cannot leak a roadmap it never saw —
defense in depth, not just a prompt instruction.

The post is written in Kunal's personal founder voice (embedded here,
not read from a kit file — same cloud-reliability reason as the email
drafter's voice.py). A postability gate returns worth_posting=false
rather than forcing a weak post out of a thin or internal-only briefing
(the content equivalent of the inbox noise filter).

Output lands in creator_artifacts (kind='linkedin_post',
status='pending_review') for Kunal to review → approve(=ship) → post
manually. Astra never posts to LinkedIn on his behalf.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from sqlalchemy import text

from astra.creators._shared import generate_json
from astra.creators.store import (
    create_artifact,
    get_artifact,
    update_artifact_content,
)
from astra.db.engine import async_session

logger = logging.getLogger(__name__)


# Sections of a research briefing that are OUTWARD-FACING (safe to base a
# public post on). Everything else — Build, Subtract, Urgent, Action
# items — is Kunal's internal roadmap and is stripped before drafting.
_OUTWARD_SECTIONS = {"findings", "signals", "sources"}
_INTERNAL_SECTIONS = {"build", "subtract", "urgent", "action items", "action-items"}


_LINKEDIN_VOICE = """You draft a single LinkedIn post AS Kunal — a founder building in AI.

WHO KUNAL IS (this shapes the VOICE and PERSPECTIVE; it is NOT material to
recite in the post): a founder building India's execution-first AI layer,
who also runs a premium apparel company and trains as a competitive squash
athlete. His ambition is to become one of the strongest AI builders in the
world and a technology decision-maker for India. He thinks like an operator
who ships, not a commentator who reacts.

THE VOICE
- First person, declarative, execution-first. He states a view and backs it.
- Specific over abstract. Concrete examples beat adjectives.
- No hype, no buzzword soup, no "thrilled/humbled/excited to announce", no
  emoji spam, no "in today's fast-moving landscape". Plain, sharp,
  Indian-English founder register.
- Opinionated. A post that could have been written by anyone is a failure.
  Take a real stance a smart reader might push back on.
- Reads like a sharp operator thinking out loud — not a marketer.

HARD RULES (violating any one ruins the post)
1. PUBLIC TAKE, NOT A STATUS UPDATE. The post is Kunal's POV on what is
   happening in the field — drawn ONLY from the market insight provided.
   NEVER write "we're building / I'm shipping / we just launched / our
   roadmap". NEVER name or imply any internal tool, agent, system, or
   product of his. If a sentence reads as a personal product announcement
   or a company status report, delete it.
2. NO RÉSUMÉ-DROPPING. Do NOT state his rank, credentials, company names,
   biography, or "as someone who has...". His authority must come through
   the sharpness of the take, not name-dropping himself. (Persona informs
   voice only.)
3. NO FABRICATION. Do not invent numbers, quotes, companies, dates, or
   events. Ground every claim in the provided market insight. If a number
   would help but isn't provided, write without it.
4. EARN THE STANCE. The post must contain one genuine insight or argument,
   not a summary of news. "Here's what happened" is weak; "here's what it
   means and why most people are reading it wrong" is the bar.
5. ABSOLUTE — NO SELF-REFERENCE, NO PRIVATE METRICS. The market insight you
   are given is extracted from Kunal's PRIVATE strategy brief. It may carry
   numbers and status about HIS OWN systems, tools, training, tasks, or
   usage. NONE of that is ever post content. The post is about the FIELD and
   the MARKET — external developments only.
   - NEVER write "my/our stack", "our system", "our agent layer", "looking
     at our own X", "zero turns", "N overdue tasks", "N scheduler jobs",
     "my training", "week-over-week", or ANY number describing Kunal's own
     operations or progress.
   - NEVER name or imply his internal tools, agent, or system.
   - NEVER make the post a confession about his own usage, backlog, or pace.
   BAD — absolutely forbidden: "Meanwhile I'm looking at our own stack: 22
   jobs, zero agent turns this week, 84 overdue tasks." That leaks private
   state. GOOD: a sharp external read on what the developments mean for
   anyone building in the space.

FORMAT
- hook: the first line. <=120 chars. A claim, a tension, a contrarian read,
  or a sharp question. NEVER "Here are N things" or "Excited to share".
- body: 3–6 short paragraphs, a blank line between each (LinkedIn is
  scanned, not read). One idea per paragraph. Land a clear point of view.
  Close with a crisp takeaway OR one genuine question — not both.
- hashtags: 2–4, relevant and discoverable. No hashtag spam.
- length: roughly 700–1900 characters of body. Tight beats long.

POSTABILITY GATE
If the provided market insight is too thin, purely operational, or has no
genuine public-worthy angle a smart audience would stop scrolling for,
return worth_posting=false with a one-line reason. Skipping beats filler.

Return STRICT JSON, no prose, no markdown fences:
{
  "worth_posting": true | false,
  "reason": "<if false: why. if true: the one-line outward angle.>",
  "title": "<short internal title for records, <=80 chars>",
  "hook": "<the first line>",
  "body": "<the FULL post text including the hook, with \\n\\n between paragraphs, NO hashtags>",
  "hashtags": ["#Tag", ...],
  "engagement_prompt": "<optional closing question, or empty string>",
  "char_count": <integer length of body>
}"""


def _post_text_blob(d: dict[str, Any]) -> str:
    """Everything that should be scanned for a kit's forbidden phrases."""
    parts = [str(d.get("hook", "")), str(d.get("body", ""))]
    parts.extend(str(h) for h in (d.get("hashtags") or []))
    parts.append(str(d.get("engagement_prompt", "")))
    return "\n".join(parts)


# ── Internal-state leak guard ──────────────────────────────────────
#
# The research briefing is compass + telemetry aware: even its outward
# sections can carry numbers about Kunal's OWN systems, training, tasks,
# and usage. Stripping internal SECTIONS isn't enough — a leak can ride
# inside a finding. So we scan the GENERATED post and regenerate (then
# suppress) if it self-references. Verify the output, not just the input
# — same discipline as the brief fact-guard. Found on the first real
# prod run: a post that broadcast "22 jobs, zero agent turns this week,
# 84 overdue tasks, training flat."
_LEAK_SUBSTRINGS = (
    "our stack", "my stack", "our own stack", "our system", "my system",
    "our agent", "my agent layer", "our agent layer", "our codebase",
    "looking at our own", "our own system", "our own infra",
    "agent turns", "scheduler jobs", "episodic memories", "overdue tasks",
    "catchup approval", "catch-up approval", "catchup approvals",
    "training data is flat", "my training", "training catchup", "betterstack",
    "week-over-week", "week over week", "astra",
)
_LEAK_REGEX = (
    re.compile(r"\b\d+\s+(?:overdue\s+)?(?:tasks|jobs|turns|memories|approvals|sessions)\b", re.I),
    re.compile(r"\bzero\s+(?:agent\s+)?(?:turns|tasks|approvals|sessions)\b", re.I),
)


def _leaks_internal(text_blob: str) -> list[str]:
    """Return the internal-state markers a drafted post leaked, if any."""
    low = (text_blob or "").lower()
    hits = [s for s in _LEAK_SUBSTRINGS if s in low]
    for rx in _LEAK_REGEX:
        m = rx.search(text_blob or "")
        if m:
            hits.append(m.group(0))
    return hits


# Mined voice profile (from Kunal's real sent mail, via the email
# agent's /voice/profile). Module-cached ~1h; a dead email agent or an
# empty profile just means the hand-written voice stands alone.
_mined_voice_cache: dict[str, Any] = {"text": "", "at": None}


async def _mined_voice_addendum() -> str:
    import os
    import time

    if (_mined_voice_cache["at"] is not None
            and time.monotonic() - _mined_voice_cache["at"] < 3600):
        return _mined_voice_cache["text"]
    text_out = ""
    try:
        import httpx

        base = os.environ.get(
            "EMAIL_AGENT_URL", "http://email.railway.internal:8080"
        ).rstrip("/")
        headers = {
            "x-astra-secret": os.environ.get("AGENT_SHARED_SECRET", "").strip()
        }
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.get(
                f"{base}/api/v1/voice/profile",
                params={"register": "general"},
                headers=headers,
            )
        if r.status_code == 200:
            profile = ((r.json() or {}).get("profile") or "").strip()
            if profile:
                text_out = (
                    "\n\nHOW KUNAL ACTUALLY WRITES (mined from his real sent "
                    "mail — let this shape sentence rhythm, directness and "
                    "phrasing; the post format rules above still apply):\n"
                    + profile[:1800]
                )
    except Exception as e:
        logger.info("[linkedin] mined voice unavailable: %s", e)
    _mined_voice_cache.update(text=text_out, at=time.monotonic())
    return text_out


async def _generate_guarded(
    *, user: str, forbidden: list[str]
) -> tuple[dict[str, Any], list[str]]:
    """Draft via generate_json, scan for an internal-state leak, and
    regenerate ONCE with a hard warning if it leaked. Returns
    (post, residual_leaks) — residual_leaks non-empty means even the
    retry leaked and the caller must NOT stage it."""
    system = _LINKEDIN_VOICE + await _mined_voice_addendum()
    post = await generate_json(
        system=system,
        user=user,
        forbidden=forbidden,
        text_blob_fn=_post_text_blob,
    )
    leaks = _leaks_internal(_post_text_blob(post))
    if not leaks:
        return post, []
    logger.warning("[linkedin] internal-state leak, regenerating: %s", leaks[:6])
    warn = (
        "\n\n<leak-warning>\nYour previous draft LEAKED private internal "
        f"state: {leaks[:8]}. This is forbidden. Rewrite as a take on the "
        "MARKET / FIELD only — ZERO references to Kunal's own systems, "
        "tools, metrics, training, tasks, backlog, usage, or any number "
        "about his operations. Same JSON schema.\n</leak-warning>"
    )
    post = await generate_json(
        system=system,
        user=user + warn,
        forbidden=forbidden,
        text_blob_fn=_post_text_blob,
    )
    return post, _leaks_internal(_post_text_blob(post))


def _extract_outward(body_md: str) -> str:
    """Strip a research briefing down to its outward-facing insight.

    Keeps the title, the **Gist.** line, and the Findings / Signals /
    Sources sections. Drops Build / Subtract / Urgent / Action items —
    Kunal's internal roadmap, which must never reach a public post.
    """
    lines = (body_md or "").splitlines()
    out: list[str] = []
    keep = True  # title + gist (before the first ## section) are kept
    for ln in lines:
        stripped = ln.strip()
        if stripped.startswith("## "):
            name = stripped[3:].strip().lower()
            keep = name in _OUTWARD_SECTIONS
            if keep:
                out.append(ln)
            continue
        if keep:
            out.append(ln)
    return "\n".join(out).strip()


async def _get_briefing(briefing_id: int) -> dict[str, Any] | None:
    async with async_session() as s:
        r = await s.execute(
            text(
                """
                SELECT id, topic, kind, status, body_md, business_tags
                FROM research_briefings WHERE id = :id
                """
            ),
            {"id": int(briefing_id)},
        )
        row = r.first()
    if not row:
        return None
    return {
        "id": row[0], "topic": row[1], "kind": row[2],
        "status": row[3], "body_md": row[4] or "", "business_tags": row[5] or "",
    }


async def _get_latest_ready_briefing() -> dict[str, Any] | None:
    """The most recent publishable briefing. Excludes meta_review (the
    Saturday self-audit) — that's purely internal, never post material."""
    async with async_session() as s:
        r = await s.execute(
            text(
                """
                SELECT id, topic, kind, status, body_md, business_tags
                FROM research_briefings
                WHERE status = 'ready'
                  AND COALESCE(kind, '') NOT IN ('meta_review', 'meta')
                  AND COALESCE(topic, '') NOT ILIKE '%meta-review%'
                ORDER BY created_at DESC
                LIMIT 1
                """
            )
        )
        row = r.first()
    if not row:
        return None
    return {
        "id": row[0], "topic": row[1], "kind": row[2],
        "status": row[3], "body_md": row[4] or "", "business_tags": row[5] or "",
    }


async def _existing_post_for_briefing(briefing_id: int) -> int | None:
    """Idempotency: the artifact id of an existing LinkedIn post for this
    briefing, or None. Keyed on content->>'research_briefing_id'."""
    async with async_session() as s:
        r = await s.execute(
            text(
                """
                SELECT id FROM creator_artifacts
                WHERE kind = 'linkedin_post'
                  AND content->>'research_briefing_id' = :bid
                ORDER BY created_at DESC LIMIT 1
                """
            ),
            {"bid": str(int(briefing_id))},
        )
        row = r.first()
    return int(row[0]) if row else None


def _kit_forbidden(business_tags: str) -> list[str]:
    """Best-effort forbidden-phrase list from the matching business kit.
    Voice is embedded; the kit is only consulted for brand bans."""
    slug = (business_tags or "").split(",")[0].strip().lower()
    if slug not in {"helmtech", "apex", "bay", "top-studios", "top_studios"}:
        return []
    try:
        from astra.creators.kits import load_kit

        kit = load_kit(slug.replace("_", "-"))
        return kit.brand.get("forbidden_phrases", []) or []
    except Exception:
        return []


async def draft_linkedin_post(
    *,
    briefing_id: int | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Draft (and stage) a LinkedIn post from a research briefing.

    Returns a result dict:
      {ok, status, artifact_id?, title?, reason?}
    where status is one of: 'staged' (post created, pending_review),
    'not_postable' (gate said no), 'exists' (already drafted, not forced),
    'no_briefing' (nothing ready), 'error'.
    """
    briefing = (
        await _get_briefing(briefing_id)
        if briefing_id is not None
        else await _get_latest_ready_briefing()
    )
    if not briefing:
        return {"ok": False, "status": "no_briefing"}
    if briefing["status"] != "ready":
        return {"ok": False, "status": "no_briefing", "reason": f"briefing {briefing['id']} not ready"}

    if not force:
        existing = await _existing_post_for_briefing(briefing["id"])
        if existing:
            return {"ok": True, "status": "exists", "artifact_id": existing}

    outward = _extract_outward(briefing["body_md"])
    if len(outward.strip()) < 80:
        return {
            "ok": True, "status": "not_postable",
            "reason": "briefing has no outward-facing insight to post",
        }

    user_prompt = (
        f"<topic>{briefing['topic']}</topic>\n\n"
        f"<market-insight note=\"outward-facing only; internal roadmap already stripped\">\n"
        f"{outward[:7000]}\n"
        f"</market-insight>\n\n"
        "Draft Kunal's LinkedIn post from this market insight. Form a real "
        "point of view; do not just summarize. Return JSON only."
    )

    try:
        post, residual_leaks = await _generate_guarded(
            user=user_prompt,
            forbidden=_kit_forbidden(briefing["business_tags"]),
        )
    except Exception as e:
        logger.exception("[linkedin] draft failed for briefing %s", briefing["id"])
        return {"ok": False, "status": "error", "reason": str(e)[:300]}

    if residual_leaks:
        # Even the retry leaked private state — suppress rather than risk
        # publishing his internal metrics. Better no post than that one.
        logger.error(
            "[linkedin] suppressed leaking post for briefing %s: %s",
            briefing["id"], residual_leaks[:6],
        )
        return {
            "ok": True, "status": "not_postable",
            "reason": f"suppressed — could not remove internal-state leak ({residual_leaks[:3]})",
        }

    if not post.get("worth_posting", False):
        return {
            "ok": True, "status": "not_postable",
            "reason": post.get("reason", "model declined to post"),
        }

    # Stage it for review. Stamp the briefing linkage into content so the
    # surface can show provenance and idempotency holds.
    content = {
        **post,
        "research_briefing_id": str(briefing["id"]),
        "briefing_topic": briefing["topic"],
        "business_tags": briefing["business_tags"],
        "platform": "linkedin",
    }
    title = (post.get("title") or briefing["topic"])[:120]
    slug = (briefing["business_tags"] or "personal").split(",")[0].strip() or "personal"
    artifact = await create_artifact(
        business_slug=slug,
        kind="linkedin_post",
        audience_slug="founder_personal",
        title=title,
        ask=f"LinkedIn post from research: {briefing['topic']}",
        content=content,
        status="pending_review",
    )
    logger.info(
        "[linkedin] staged post %s from briefing %s (%s)",
        artifact["id"], briefing["id"], title,
    )
    return {
        "ok": True, "status": "staged",
        "artifact_id": artifact["id"], "title": title,
        "reason": post.get("reason", ""),
    }


async def refine_linkedin_post(
    artifact_id: int, instruction: str
) -> dict[str, Any]:
    """Revise a staged LinkedIn post per Kunal's instruction, keeping his
    voice + the hard rules. Stays pending_review (does not post). Returns
    the updated artifact dict. Raises ValueError if not found / wrong kind."""
    art = await get_artifact(artifact_id)
    if not art or art.get("kind") != "linkedin_post":
        raise ValueError(f"no linkedin_post artifact {artifact_id}")
    content = art.get("content") or {}

    user = (
        "<current-post>\n"
        f"Hook: {content.get('hook', '')}\n"
        f"Body:\n{content.get('body', '')}\n"
        f"Hashtags: {', '.join(content.get('hashtags') or [])}\n"
        "</current-post>\n\n"
        f"<instruction>{instruction}</instruction>\n\n"
        "Revise the post per the instruction. Keep Kunal's voice and ALL the "
        "hard rules (public take, no résumé-dropping, no fabrication, no "
        "internal/status content). Set worth_posting=true. Return JSON only."
    )
    post, residual_leaks = await _generate_guarded(
        user=user,
        forbidden=_kit_forbidden(content.get("business_tags", "")),
    )
    if residual_leaks:
        raise RuntimeError(
            f"refine produced an internal-state leak ({residual_leaks[:3]}); "
            "not saving"
        )
    # Preserve provenance keys; overlay the revised post fields.
    new_content = {**content, **post}
    new_content["worth_posting"] = True
    title = (post.get("title") or art.get("title") or "")[:120]
    await update_artifact_content(
        artifact_id, content=new_content, status="pending_review", title=title
    )
    return {**art, "content": new_content, "title": title, "status": "pending_review"}


async def content_metrics(days: int = 7) -> dict[str, Any]:
    """The Friday number for the content beachhead: over the window, how
    many posts were drafted, approved (=shipped intent), posted (with a
    URL), rejected, still pending — plus approval rate + posts/week."""
    async with async_session() as s:
        r = await s.execute(
            text(
                """
                SELECT status, COUNT(*)
                FROM creator_artifacts
                WHERE kind = 'linkedin_post'
                  AND created_at >= now() - (:days || ' days')::interval
                GROUP BY status
                """
            ),
            {"days": int(days)},
        )
        counts = {row[0]: int(row[1]) for row in r.all()}
    drafted = sum(counts.values())
    posted = counts.get("posted", 0)
    approved = counts.get("approved", 0) + posted  # posted implies approved
    rejected = counts.get("rejected", 0)
    pending = counts.get("pending_review", 0)
    decided = approved + rejected
    rate = round(approved / decided, 3) if decided else None
    posts_per_week = round(approved / max(1, days) * 7, 1)
    return {
        "window_days": days,
        "drafted": drafted,
        "approved": approved,
        "posted": posted,
        "rejected": rejected,
        "pending": pending,
        "approval_rate": rate,
        "posts_per_week": posts_per_week,
    }
