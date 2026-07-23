"""
E1 — the engagement slate: feed ingest → candidate selection → comment
drafts in Kunal's confirmed voice → approval slate.

Kunal's model (2026-07-22): the Mac reader walks his OPEN logged-in
X/LinkedIn feeds on a schedule and posts batches here. The cloud picks
the posts worth engaging, drafts a comment for each in HIS voice (the
spec, not a brand voice), and stages them for approval. For ~2 weeks
every verdict (approve / edit / skip) is training data; only then do
permission modes graduate toward semi-autonomous engagement (E2).

E1 never engages by itself: approved comments ride the same Sidecar
publish queue as posts (/content/next-approved) — Kunal pastes with
one key and clicks reply himself.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from astra.db.engine import async_session

logger = logging.getLogger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))

_ENSURE_SQL = """
CREATE TABLE IF NOT EXISTS engagement_feed (
    id            BIGSERIAL PRIMARY KEY,
    platform      TEXT NOT NULL,
    post_url      TEXT NOT NULL UNIQUE,
    author        TEXT NOT NULL DEFAULT '',
    author_handle TEXT NOT NULL DEFAULT '',
    text          TEXT NOT NULL DEFAULT '',
    metrics       JSONB NOT NULL DEFAULT '{}'::jsonb,
    status        TEXT NOT NULL DEFAULT 'new',
    batch_id      TEXT NOT NULL DEFAULT '',
    seen_at       TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


async def _ensure_table() -> None:
    """Point-of-use guard (same pattern as training_counters) so the
    stream service works even if it deploys before the scheduler runs
    the alembic migration. Identical SQL to migration v0o36p2n7k9k."""
    async with async_session() as s:
        await s.execute(text(_ENSURE_SQL))
        await s.commit()


async def ingest_feed(
    items: list[dict[str, Any]], *, batch_id: str = "",
) -> dict[str, int]:
    """Upsert scraped feed posts. Dedupe on post_url — repeated scrolls
    refresh metrics/seen_at instead of duplicating. Returns counts."""
    await _ensure_table()
    new = updated = skipped = 0
    async with async_session() as s:
        for it in items:
            url = str(it.get("post_url") or "").strip()
            platform = str(it.get("platform") or "").strip().lower()
            body = str(it.get("text") or "").strip()
            if not url.startswith("http") or platform not in ("x", "linkedin") or len(body) < 20:
                skipped += 1
                continue
            r = await s.execute(
                text("""
                    INSERT INTO engagement_feed
                        (platform, post_url, author, author_handle, text,
                         metrics, batch_id)
                    VALUES (:p, :u, :a, :h, :t, CAST(:m AS JSONB), :b)
                    ON CONFLICT (post_url) DO UPDATE SET
                        metrics = CAST(:m AS JSONB),
                        seen_at = now()
                    RETURNING (xmax = 0) AS inserted
                """),
                {
                    "p": platform, "u": url[:900],
                    "a": str(it.get("author") or "")[:200],
                    "h": str(it.get("author_handle") or "")[:200],
                    "t": body[:4000],
                    "m": json.dumps(it.get("metrics") or {})[:2000],
                    "b": batch_id[:80],
                },
            )
            if r.scalar():
                new += 1
            else:
                updated += 1
        await s.commit()
    logger.info("[engagement] ingest: %d new, %d updated, %d skipped",
                new, updated, skipped)
    return {"new": new, "updated": updated, "skipped": skipped}


_SELECT_PROMPT = """You select which feed posts Kunal Singh should engage with (comment) today. Kunal: founder building an AI creative production platform under his AI company, runs a premium apparel brand and a squash venture, competitive squash player. His public presence: a standard-bearer speaking as an expert in what he has built and mastered.

Pick posts where he can ADD something real: a mechanism he understands first-hand, a sharp counter-read, a specific experience from building. Skip: politics, rage-bait, personal drama, giveaway/engagement-bait, anything where a comment could be quoted against him, and posts he could only reply "great post" to.

FEED POSTS (JSON):
{posts}

STRICT JSON only — pick at most {k}, ranked:
{{"picks": [{{"id": <feed id>, "why": "<one line: the angle he uniquely has>", "risk": "<none|low|note>"}}]}}
Empty picks if nothing merits engagement — an empty slate beats a forced one."""


_COMMENT_PROMPT = """Draft the comment Kunal will post as a reply to this {platform} post.

TARGET POST by {author}:
<<<{target}>>>

THE ANGLE (why he is engaging): {why}

COMMENT RULES (on top of the voice spec):
- 1-3 sentences. Comments are sharper and smaller than posts.
- ADD something: the mechanism, the first-hand observation, the precise
  counter-read. Zero value-add = do not force it, return empty comment.
- Never "Great post", never flattery, never restating the post back.
- No self-promotion, no links, no hashtags, no emojis.
- Disagreement is fine and welcome when he has the standing; it stays
  about the substance, never the person.
- A claim about the external world needs to be one he can defend; when
  in doubt, write from experience instead of statistics.

STRICT JSON only:
{{"comment": "<the comment, or empty string if nothing worth adding>",
"makes_factual_claim": <true|false>}}"""


async def build_slate(*, limit: int = 6, notify: bool = True) -> dict[str, Any]:
    """Select candidates from fresh feed rows, draft comments in Kunal's
    voice, stage as engagement_comment artifacts, optionally WhatsApp-
    nudge the slate. Factual comments pass the claim gate (fail closed:
    unsupported claim → candidate dropped, feed row back to 'new')."""
    from astra.creators._shared import generate_json
    from astra.creators.kunal_public_voice import VOICE_SPEC
    from astra.creators.store import create_artifact

    await _ensure_table()
    async with async_session() as s:
        r = await s.execute(
            text("""
                SELECT id, platform, post_url, author, author_handle,
                       text, metrics
                FROM engagement_feed
                WHERE status = 'new'
                  AND seen_at > now() - interval '48 hours'
                ORDER BY seen_at DESC
                LIMIT 60
            """)
        )
        rows = r.all()
    if not rows:
        return {"ok": True, "staged": 0, "reason": "no fresh feed posts"}

    posts_json = json.dumps(
        [{"id": row[0], "platform": row[1], "author": row[3],
          "handle": row[4], "text": row[5][:600], "metrics": row[6]}
         for row in rows],
        indent=0,
    )[:14000]
    sel = await generate_json(
        system="You are a sharp editorial selector. STRICT JSON only.",
        user=_SELECT_PROMPT.format(posts=posts_json, k=max(1, min(8, limit))),
        forbidden=[],
        text_blob_fn=lambda d: "",
    )
    picks = [p for p in (sel.get("picks") or [])
             if isinstance(p, dict) and p.get("id")]
    by_id = {row[0]: row for row in rows}
    staged: list[dict[str, Any]] = []
    dropped: list[str] = []

    for p in picks[:limit]:
        row = by_id.get(p["id"])
        if row is None:
            continue
        fid, platform, url, author, handle, body, _metrics = row
        try:
            draft = await generate_json(
                system=VOICE_SPEC,
                user=_COMMENT_PROMPT.format(
                    platform="X" if platform == "x" else "LinkedIn",
                    author=author or handle or "unknown",
                    target=body[:2000],
                    why=p.get("why", ""),
                ),
                forbidden=[],
                text_blob_fn=lambda d: str(d.get("comment", "")),
            )
        except Exception as e:
            logger.warning("[engagement] comment draft failed for %s: %s", fid, e)
            continue
        comment = (draft.get("comment") or "").strip()
        if not comment:
            continue

        claim_report: list[dict[str, Any]] = []
        if draft.get("makes_factual_claim"):
            from astra.research.agent import claim_check

            today = datetime.now(_IST).strftime("%Y-%m-%d")
            try:
                claim_report = await claim_check(
                    [comment],
                    context=f"a public {platform} comment by a founder",
                    today=today,
                )
            except Exception as e:
                logger.warning("[engagement] claim check errored: %s", e)
                dropped.append(f"{fid}: claim check errored")
                continue
            if any(c["verdict"] in ("CONTRADICTED", "UNSUPPORTED")
                   for c in claim_report):
                dropped.append(f"{fid}: unsupported factual claim")
                continue

        art = await create_artifact(
            business_slug="personal",
            kind="engagement_comment",
            audience_slug="founder_personal",
            title=f"reply to {author or handle} ({platform})"[:120],
            ask=f"engagement comment on {url}",
            content={
                "platform": platform,
                "body": comment,
                "target_url": url,
                "target_author": author or handle,
                "target_text": body[:1200],
                "why": p.get("why", ""),
                "claim_report": claim_report,
            },
            status="pending_review",
        )
        staged.append({"artifact_id": art["id"], "author": author or handle,
                       "platform": platform, "comment": comment,
                       "target_url": url, "target_text": body[:200]})
        async with async_session() as s:
            await s.execute(
                text("UPDATE engagement_feed SET status='slated' WHERE id=:i"),
                {"i": fid},
            )
            await s.commit()

    if staged and notify:
        await _notify_slate(staged)
    logger.info("[engagement] slate: %d staged, %d dropped", len(staged), len(dropped))
    return {"ok": True, "staged": len(staged), "dropped": dropped,
            "items": staged}


async def _notify_slate(staged: list[dict[str, Any]]) -> None:
    """WhatsApp the slate through the gateway (owner-window aware on the
    gateway side). Failure is logged loudly, never swallowed silently —
    a silently-lost slate is the drafts-rotting failure all over again."""
    import httpx

    base = os.environ.get(
        "GATEWAY_URL", "http://whatsapp.railway.internal:8080"
    ).rstrip("/")
    secret = os.environ.get("AGENT_SHARED_SECRET", "").strip()
    lines = [f"Engagement slate — {len(staged)} comment(s) ready:"]
    for s_item in staged:
        snippet = (s_item.get("target_text") or "").replace("\n", " ")[:120]
        lines.append(
            f"\n#{s_item['artifact_id']} → {s_item['author']} "
            f"({s_item['platform']})\n"
            f"THEIR POST: {snippet}…\n"
            f"{s_item.get('target_url', '')}\n"
            f"COMMENT: \"{s_item['comment']}\""
        )
    lines.append(
        "\nReply: approve <id> / refine <id> <note> / skip <id>. "
        "Approved comments queue for ⌥⌘L paste."
    )
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.post(
                f"{base}/api/v1/notify/owner",
                json={"text": "\n".join(lines)[:3500]},
                headers={"x-astra-secret": secret},
            )
        if r.status_code != 200:
            logger.error("[engagement] slate notify FAILED: %s %s",
                         r.status_code, r.text[:200])
    except Exception:
        logger.exception("[engagement] slate notify errored")
