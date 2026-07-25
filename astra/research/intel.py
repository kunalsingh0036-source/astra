"""
The daily intelligence brief — the North Star function.

Composed ONLY from spine-fetched items (astra/research/spine.py), never
open web search: every candidate fact arrives with a URL Astra itself
fetched from a registered source, which makes fabricated citations
structurally impossible. Delta-aware: the composer receives the last 30
days of already-briefed claims (intel_items) as a KNOWN block and must
not re-report them except as explicit updates.

Format contract (Kunal, 2026-07-25): WhatsApp gets ONE SCREEN —
indicative headlines + one so-what line each + a link into the app's
Research room. "No one studies on WhatsApp." The full brief lives in
the PWA (research_briefings row, kind='intel_daily').

Non-generic mechanisms, enforced in schema/code not vibes:
- every LEAD must climb the ladder: what happened → what changed →
  so-what for a NAMED business (Sargo/HelmTech/Astra/BAY/Apex) →
  do/watch by when; items that can't climb go to the delta log.
- lead: [] is LEGAL and renders "Quiet day. No action." — padding
  pressure removed at the schema level.
- every cited URL must be one of the provided source_items URLs —
  checked in code after composition; violations are dropped.
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

# Priority Intelligence Requirements — what "important" means for Kunal.
_PIRS = """PRIORITY INTELLIGENCE REQUIREMENTS (score items against these; 0-on-all = drop):
1. Sargo (AI creative studio, Aug 1 relaunch): video/image/audio model releases, pricing shifts, marketplace additions (fal/Replicate), competitor moves (Higgsfield/InVideo/Pika/Runway).
2. HelmTech sovereign-AI thesis: open-weights frontier moves (DeepSeek/Qwen/Moonshot/Meta), India AI policy (MeitY/IndiaAI/DPDP), compute economics.
3. Astra + Shotgun architecture: agent frameworks, MCP ecosystem, Claude/Anthropic releases, inference stack (vLLM/SGLang) shifts.
4. The founder race: major funding rounds in creative-AI or agents, enterprise adoption signals, India startup ecosystem moves.
5. AI mastery curriculum: genuinely important papers/techniques a top AI founder must know this week.
"""

_COMPOSE_PROMPT = """You are Astra's intelligence officer composing the daily brief for Kunal Singh (founder: Sargo AI creative studio under HelmTech; Astra agent OS; Apex apparel; BAY squash; ambition: top AI mind + tech decision-maker for India). Today (IST): {today}.

You may use ONLY the source items below — each was fetched by our own pipeline from a registered source in the last 36 hours. You may NOT introduce any fact, number, or URL not present in them. If the day is thin, say so; a quiet day honestly reported beats filler.

{pirs}

ALREADY BRIEFED (last 30 days — do NOT re-report; if an item below is an update to one of these, frame it explicitly as "update to <claim>, first briefed <date>"):
{known}

SOURCE ITEMS (last 36h, JSON):
{items}

Compose STRICT JSON:
{{"lead": [
   {{"headline": "<what happened, one sharp line>",
     "changed": "<what actually changed vs the prior state of the world>",
     "so_what": "<the implication for a NAMED business: Sargo/HelmTech/Astra/Apex/BAY — concrete, not 'this is interesting'>",
     "action": "<do/watch + by-when, or 'watch' if genuinely nothing to do>",
     "url": "<the source item URL this is built on>",
     "entities": "<comma-separated: lab/model/company/policy names>"}}
 ],
 "delta_log": [
   {{"line": "<one-liner: thing that moved, worth knowing, not lead-worthy>", "url": "<source url>", "entities": "<names>"}}
 ],
 "quiet": <true if lead is empty>,
 "read_next": "<if one source item deserves 20 minutes of Kunal's study time today, name it + why in one line; else empty string>"}}

Rules:
- lead: 0-3 items MAX. delta_log: 0-6. An empty lead with a short delta_log is a GOOD output on a quiet day.
- A lead item that cannot name a business in so_what is not a lead item — demote it.
- Discourse-tier items (newsletters, HN, reddit) can support or corroborate but rarely lead on their own; primary-source items (lab posts, model releases, papers, policy) lead.
- Assume Kunal is a well-read AI founder: no explaining what transformers are, no "AI is moving fast" filler, no hype adjectives.
- Write headlines and so-whats in his register: short declarative sentences, no em-dashes, no exclamation marks."""


async def run_daily_intel() -> dict[str, Any]:
    """The 07:00 IST intel run. Returns {briefing_id, lead_count, ...}."""
    from astra.creators._shared import generate_json
    from astra.research.runner import _insert_pending, _mark_error, _mark_ready
    from astra.research.spine import ensure_tables

    await ensure_tables()
    today = datetime.now(_IST).strftime("%a %d %b %Y")
    briefing_id = await _insert_pending(
        topic=f"AI Intel — {today}", kind="intel_daily", business_tags="helmtech",
    )
    t0 = datetime.now(timezone.utc)
    try:
        async with async_session() as s:
            r = await s.execute(text("""
                SELECT source_key, tier, tag, title, url, summary, published
                FROM source_items
                WHERE fetched_at > now() - interval '36 hours'
                ORDER BY tier ASC, fetched_at DESC
                LIMIT 120
            """))
            rows = r.all()
            k = await s.execute(text("""
                SELECT claim, briefed_at::date
                FROM intel_items
                WHERE briefed_at > now() - interval '30 days'
                ORDER BY briefed_at DESC LIMIT 80
            """))
            known_rows = k.all()

        if not rows:
            await _mark_error(briefing_id, "no spine items in window (fetch job dry?)", "", t0)
            return {"status": "error", "id": briefing_id, "error": "no_spine_items"}

        items_json = json.dumps(
            [{"src": r0[0], "tier": r0[1], "tag": r0[2], "title": r0[3],
              "url": r0[4], "summary": r0[5][:300], "published": r0[6]}
             for r0 in rows], indent=0)[:30000]
        known = "\n".join(f"- {kr[0]} (briefed {kr[1]})" for kr in known_rows) or "(nothing briefed yet)"

        parsed = await generate_json(
            system="You are a precise intelligence officer. STRICT JSON only.",
            user=_COMPOSE_PROMPT.format(today=today, pirs=_PIRS, known=known,
                                        items=items_json),
            forbidden=[],
            text_blob_fn=lambda d: "",
        )

        # CODE GATE: every cited URL must be a real spine URL.
        valid_urls = {r0[4] for r0 in rows}
        leads = [l for l in (parsed.get("lead") or [])
                 if isinstance(l, dict) and l.get("url") in valid_urls]
        dropped_leads = len(parsed.get("lead") or []) - len(leads)
        deltas = [d for d in (parsed.get("delta_log") or [])
                  if isinstance(d, dict) and d.get("url") in valid_urls]
        if dropped_leads:
            logger.warning("[intel] dropped %d lead(s) citing non-spine URLs", dropped_leads)

        body_md = _render_md(today, leads, deltas, parsed.get("read_next") or "")
        await _mark_ready(
            briefing_id=briefing_id, body_md=body_md,
            parsed={"gist": _one_screen(today, leads, deltas, briefing_id, parsed.get("read_next") or ""),
                    "findings": [], "signals": [], "sources": [
                        {"url": l.get("url"), "description": l.get("headline", "")}
                        for l in leads + deltas], "lead": leads, "delta_log": deltas},
            model_used="intel_daily", started_at=t0,
        )
        # file briefed claims into the KNOWN log
        async with async_session() as s:
            for l in leads:
                await s.execute(text("""
                    INSERT INTO intel_items (briefing_id, claim, entities, url)
                    VALUES (:b, :c, :e, :u)
                """), {"b": briefing_id, "c": (l.get("headline") or "")[:400],
                       "e": (l.get("entities") or "")[:300], "u": (l.get("url") or "")[:900]})
            for d in deltas:
                await s.execute(text("""
                    INSERT INTO intel_items (briefing_id, claim, entities, url)
                    VALUES (:b, :c, :e, :u)
                """), {"b": briefing_id, "c": (d.get("line") or "")[:400],
                       "e": (d.get("entities") or "")[:300], "u": (d.get("url") or "")[:900]})
            await s.commit()

        await _send_one_screen(today, leads, deltas, briefing_id, parsed.get("read_next") or "")
        return {"status": "ready", "id": briefing_id, "lead_count": len(leads),
                "delta_count": len(deltas), "quiet": not leads}
    except Exception as e:
        logger.exception("[intel] daily run crashed")
        await _mark_error(briefing_id, str(e)[:900], "", t0)
        return {"status": "error", "id": briefing_id, "error": str(e)[:300]}


def _render_md(today: str, leads: list, deltas: list, read_next: str) -> str:
    """The FULL brief for the Research room (PWA)."""
    parts = [f"# AI Intel — {today}\n"]
    if not leads:
        parts.append("**Quiet day. No action.**\n")
    for i, l in enumerate(leads, 1):
        parts.append(
            f"## {i}. {l.get('headline','')}\n\n"
            f"**What changed:** {l.get('changed','')}\n\n"
            f"**So what:** {l.get('so_what','')}\n\n"
            f"**Action:** {l.get('action','')}\n\n"
            f"Source: {l.get('url','')}\n"
        )
    if deltas:
        parts.append("## Delta log\n")
        for d in deltas:
            parts.append(f"- {d.get('line','')} ([source]({d.get('url','')}))")
        parts.append("")
    if read_next:
        parts.append(f"## Worth 20 minutes\n{read_next}\n")
    return "\n".join(parts)


def _one_screen(today: str, leads: list, deltas: list, briefing_id: int,
                read_next: str) -> str:
    """ONE WhatsApp screen: indicative lines + link. No study material."""
    app = os.environ.get("ASTRA_WEB_URL", "https://astra.thearrogantclub.com").rstrip("/")
    lines = [f"AI Intel — {today}"]
    if not leads:
        lines.append("Quiet day. No action.")
        if deltas:
            lines.append(f"{len(deltas)} small moves logged.")
    else:
        for i, l in enumerate(leads, 1):
            lines.append(f"{i}. {l.get('headline','')}")
            lines.append(f"   → {l.get('so_what','')[:110]}")
    if read_next:
        lines.append(f"Study: {read_next[:100]}")
    lines.append(f"Full brief: {app}/research/{briefing_id}")
    return "\n".join(lines)


async def _send_one_screen(today: str, leads: list, deltas: list,
                           briefing_id: int, read_next: str) -> None:
    import httpx

    base = os.environ.get("GATEWAY_URL", "http://whatsapp.railway.internal:8080").rstrip("/")
    secret = os.environ.get("AGENT_SHARED_SECRET", "").strip()
    msg = _one_screen(today, leads, deltas, briefing_id, read_next)
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.post(f"{base}/api/v1/notify/owner",
                             json={"text": msg[:1600]},
                             headers={"x-astra-secret": secret})
        if r.status_code != 200:
            logger.error("[intel] brief delivery FAILED: %s %s",
                         r.status_code, r.text[:200])
    except Exception:
        logger.exception("[intel] brief delivery errored")
