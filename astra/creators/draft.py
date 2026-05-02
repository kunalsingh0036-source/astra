"""
LLM-driven draft generation for decks (and later docs, one-pagers,
brand kits).

The hard constraint is voice discipline: every draft inherits
voice.md and forbidden_phrases verbatim from the kit. Generated
output is checked against forbidden_phrases post-hoc; if any appear
the model gets one regeneration attempt with explicit feedback.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import anthropic

from astra.config import settings
from astra.creators.kits import BusinessKit, load_kit
from astra.creators.store import create_artifact

logger = logging.getLogger(__name__)


# Sonnet for drafting — quality matters more than cost for a deck
# the founder is going to send to investors. Haiku is fine for
# critique passes (Phase B2).
_DRAFT_MODEL = os.environ.get("CREATOR_DRAFT_MODEL", "claude-sonnet-4-6")
_MAX_TOKENS = 8000


def _get_anthropic_key() -> str:
    """Mirror of the key-resolution dance used elsewhere in the codebase."""
    key = settings.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        env = Path(__file__).resolve().parents[2] / ".env"
        if env.exists():
            for line in env.read_text().splitlines():
                if line.startswith("ANTHROPIC_API_KEY="):
                    key = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    return key


def _check_forbidden(text_blob: str, forbidden: list[str]) -> list[str]:
    """Return the list of forbidden phrases that appear in the text.
    Case-insensitive substring match (intentionally loose — we'd
    rather over-flag than miss "AI-Powered" vs "AI-powered")."""
    if not forbidden:
        return []
    lower = text_blob.lower()
    return [p for p in forbidden if p.lower() in lower]


def _slide_text(slides: list[dict[str, Any]]) -> str:
    """Flatten all slide text into one blob for forbidden-phrase scanning."""
    parts: list[str] = []
    for s in slides:
        for k in ("title", "subtitle", "heading", "body_md", "footer", "caption"):
            v = s.get(k)
            if isinstance(v, str):
                parts.append(v)
        # Bullets / lists
        for k in ("bullets", "items"):
            v = s.get(k)
            if isinstance(v, list):
                parts.extend(str(x) for x in v)
    return "\n".join(parts)


# ── deck ────────────────────────────────────────────────────────────


_DECK_SYSTEM = """You are Astra's creator sub-agent — specifically the deck-drafting role.

You produce slide decks for one of Kunal's portfolio companies (or a
client kit drafted via Top Studios). You have ONE non-negotiable
constraint: the voice rules in <voice-rules> are absolute. The
<forbidden-phrases> list is a hard ban — if any phrase from that
list appears in your output, the deck will be rejected and
regenerated, wasting a turn.

Your output is STRICT JSON matching this schema:

{
  "title": "<title slide title — short, declarative>",
  "subtitle": "<one-line subtitle — the tagline or thesis>",
  "slides": [
    {
      "type": "cover" | "section" | "content" | "data" | "quote" | "close",
      "title": "<slide title — null for cover (uses parent title)>",
      "subtitle": "<optional, one line>",
      "heading": "<optional, larger than body>",
      "body_md": "<markdown body — short, declarative, voice-compliant>",
      "bullets": ["..."],   // optional; prefer body_md unless list is the right form
      "image_hint": "<one-line description of an image that would suit this slide; the renderer may fetch/generate it>",
      "footer": "<optional small footer>"
    }
  ]
}

Slide-type semantics:
  cover    — opening slide. Big title, subtitle, no body. Brand-color background.
  section  — section break. Single big title; signals chapter shift.
  content  — body slide. Title + 1–4 bullets OR a body paragraph. NOT BOTH.
  data     — number-forward slide. One hero stat + brief context.
  quote    — testimonial or framing quote. Attribution required.
  close    — closing slide. Contact / ask / call-to-action.

Rules:

1. Length: 8–14 slides. More is hand-waving. Fewer is incomplete. Decks for cold outreach trend short (8–10); decks for in-depth investor meetings trend longer (12–14).

2. Title slide always first; closing slide always last; section slides every 3–5 content slides to give the reader landmarks.

3. Cite proof points ONLY from the provided <proof-points>. Do not invent traction, customer names, or numbers. If a slide would benefit from data you don't have, write the slide WITHOUT the number — leave a placeholder like `[traction TBD]` so the reader knows to fill it.

4. Voice discipline: every word obeys <voice-rules>. Sentence length, hedging policy, person, active/passive — all flow from there.

5. Forbidden phrases: do not output anything in <forbidden-phrases>. Not as a quote, not in markdown, not in image_hint. The check is case-insensitive substring match.

6. Audience awareness: <audience> describes who reads this. Open the way they want to be opened. Address objections they're likely to raise. Don't waste slides on what they already know.

7. The ASK must appear explicitly on the closing slide. Not implied — stated.

Return ONLY the JSON. No prose preamble. No markdown fences. No "here is the deck:". Just the JSON object."""


async def draft_deck(
    *,
    business_slug: str,
    audience_slug: str,
    ask: str,
    context: str = "",
) -> dict[str, Any]:
    """Generate a deck and persist it. Returns the saved artifact dict.

    Args:
      business_slug: directory name under business-kits/
      audience_slug: filename (no .md) under audiences/
      ask: explicit call-to-action that must appear on the closing slide
      context: free-text additional context — recent events, news, the
               specific framing the founder wants emphasized
    """
    kit = load_kit(business_slug)
    audience_md = kit.audience(audience_slug)
    if not audience_md:
        avail = sorted(kit.audiences.keys())
        raise FileNotFoundError(
            f"audience '{audience_slug}' not found in {business_slug} kit. "
            f"Available: {avail}"
        )

    user_prompt = (
        f"{kit.render_for_prompt()}\n\n"
        f"<audience slug=\"{audience_slug}\">\n{audience_md}\n</audience>\n\n"
        f"<ask>{ask}</ask>\n\n"
    )
    if context:
        user_prompt += f"<additional-context>\n{context[:4000]}\n</additional-context>\n\n"
    user_prompt += "Draft the deck now. Return JSON only."

    forbidden = kit.brand.get("forbidden_phrases", []) or []
    deck_json = await _generate_deck_json(
        system=_DECK_SYSTEM,
        user=user_prompt,
        forbidden=forbidden,
    )

    title = deck_json.get("title") or f"{kit.name} — {ask[:60]}"
    artifact = await create_artifact(
        business_slug=business_slug,
        kind="deck",
        audience_slug=audience_slug,
        title=title,
        ask=ask,
        content=deck_json,
    )
    return artifact


async def _generate_deck_json(
    *, system: str, user: str, forbidden: list[str], _retry: bool = False
) -> dict[str, Any]:
    """Call Claude, parse JSON, retry once with feedback if forbidden phrases land."""
    key = _get_anthropic_key()
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set; cannot draft deck")
    client = anthropic.AsyncAnthropic(api_key=key)

    resp = await client.messages.create(
        model=_DRAFT_MODEL,
        max_tokens=_MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    text_out = "\n".join(
        b.text for b in resp.content if hasattr(b, "text")
    ).strip()
    # Strip code fences if the model added them despite the rule
    if text_out.startswith("```"):
        text_out = re.sub(r"^```(?:json)?|```$", "", text_out, flags=re.M).strip()

    try:
        deck = json.loads(text_out)
    except json.JSONDecodeError as e:
        logger.error("[creator] draft_deck JSON parse failed: %s", e)
        logger.error("[creator] raw response head: %s", text_out[:500])
        raise

    # Forbidden-phrase check
    slide_blob = _slide_text(deck.get("slides", []) or [])
    full_blob = (
        f"{deck.get('title','')}\n{deck.get('subtitle','')}\n{slide_blob}"
    )
    hits = _check_forbidden(full_blob, forbidden)
    if hits and not _retry:
        logger.warning(
            "[creator] forbidden phrases hit, regenerating: %s", hits
        )
        feedback = (
            "Your previous draft contained forbidden phrases: "
            f"{hits}. Rewrite the deck without using ANY of these words "
            "or any close variants. Same JSON schema."
        )
        return await _generate_deck_json(
            system=system,
            user=f"{user}\n\n<previous-attempt-feedback>\n{feedback}\n</previous-attempt-feedback>",
            forbidden=forbidden,
            _retry=True,
        )
    if hits and _retry:
        # Don't fail outright — the caller will see the deck and can
        # decide to regenerate manually. But log loudly.
        logger.error(
            "[creator] forbidden phrases STILL present after retry: %s",
            hits,
        )

    return deck
