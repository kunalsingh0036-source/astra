"""
Draft a slide deck — LLM-driven generation with voice discipline.

This is the original draft tool; it now delegates the LLM loop to
_shared.generate_json so the per-kind code stays small and consistent
with draft_one_pager / draft_doc / draft_brand_kit.

Voice discipline: every draft inherits voice.md and forbidden_phrases
verbatim from the kit. Generated output is scanned post-hoc; if any
forbidden phrase appears the model gets one regeneration attempt with
explicit feedback.
"""

from __future__ import annotations

import logging
from typing import Any

from astra.creators._shared import generate_json, join_text_fields
from astra.creators.kits import load_kit
from astra.creators.store import create_artifact

logger = logging.getLogger(__name__)


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


def _deck_text_blob(deck: dict[str, Any]) -> str:
    """Flatten all deck text for forbidden-phrase scanning."""
    parts: list[str] = []
    parts.append(deck.get("title", "") or "")
    parts.append(deck.get("subtitle", "") or "")
    for s in (deck.get("slides", []) or []):
        if isinstance(s, dict):
            parts.append(
                join_text_fields(
                    s,
                    ("title", "subtitle", "heading", "body_md",
                     "footer", "caption", "bullets", "items"),
                )
            )
    return "\n".join(parts)


async def draft_deck(
    *,
    business_slug: str,
    audience_slug: str,
    ask: str,
    context: str = "",
) -> dict[str, Any]:
    """Generate a deck and persist it. Returns the saved artifact dict."""
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
    deck_json = await generate_json(
        system=_DECK_SYSTEM,
        user=user_prompt,
        forbidden=forbidden,
        text_blob_fn=_deck_text_blob,
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
