"""
Draft a social-media carousel — slide-by-slide copy + image direction
+ caption + hashtags.

Used for LinkedIn, Instagram, X (Twitter). Each platform has slightly
different conventions (slide count, aspect ratio, caption length); the
tool takes the platform as input and tunes accordingly.

Output is JSON; render_carousel_pdf turns it into a slide-deck-style
PDF for review (and as something the founder can hand to a designer
or post directly).
"""

from __future__ import annotations

import logging
from typing import Any

from astra.creators._shared import generate_json, join_text_fields
from astra.creators.kits import load_kit
from astra.creators.store import create_artifact

logger = logging.getLogger(__name__)


_CAROUSEL_SYSTEM = """You are Astra's creator sub-agent — social-media carousel drafter.

You produce slide-by-slide carousels for LinkedIn, Instagram, or X
(Twitter). Each slide stands alone (people swipe at speed) AND the
sequence builds a narrative.

Voice rules in <voice-rules> are absolute. Forbidden phrases in
<forbidden-phrases> are a hard ban — case-insensitive substring match.

Your output is STRICT JSON matching this schema:

{
  "title": "<carousel internal title — for the founder's records, not posted>",
  "platform": "linkedin" | "instagram" | "twitter",
  "aspect_ratio": "1:1" | "4:5" | "16:9",
  "hook_promise": "<the implicit promise of slide 1 — what the swiper expects from continuing>",
  "narrative_arc": "<one sentence — how the slides build to a payoff>",
  "slides": [
    {
      "position": <integer, 1-N>,
      "type": "hook" | "context" | "claim" | "evidence" | "story" | "framework" | "stat" | "contrast" | "checklist" | "summary" | "cta",
      "headline": "<the big text on the slide — short, declarative, swipeable>",
      "body": "<supporting copy — 1-3 short sentences. May be empty for stat slides.>",
      "annotation": "<optional small text — a footnote, source, attribution>",
      "image_hint": "<concrete image direction OR 'text-only' if the slide is typography-driven>",
      "visual_treatment": "<how this slide differs visually from the others — 'inverted: dark bg, light text', 'split-screen with stat on left', 'numbered chapter divider', etc.>"
    }
  ],
  "caption": "<the post caption — the body that goes BELOW the carousel images. Voice-compliant. Includes hook + value-promise + soft CTA. NO hashtags here (they go in hashtags array).>",
  "first_comment": "<optional first-comment text — typically used on Instagram/LinkedIn to drop hashtags or links separately from the caption>",
  "hashtags": [
    "<hashtag without the # prefix>", "..."
  ],
  "best_post_time_hint": "<one short sentence — when this would land best, given audience>",
  "engagement_prompt": "<one short question to put at the end of caption to drive comments — optional>"
}

Platform conventions (apply automatically):

LinkedIn:
- Slide count: 7-12 slides (carousels go further on LinkedIn than Insta)
- Aspect: 4:5 vertical (1080x1350) — best feed real estate; 1:1 acceptable
- Caption: 1300-1800 chars works well; first 150 chars must hook (truncated in feed)
- Hashtag count: 3-5 max — LinkedIn doesn't reward more
- Tone: professional but human; thought leadership; specific industry framing
- Hooks that work: contrarian claim, "I was wrong about X", numbered list promise, before/after

Instagram:
- Slide count: 6-10 slides
- Aspect: 4:5 or 1:1
- Caption: under 2200 chars; first 125 chars before "...more" matter
- Hashtag count: 8-15 (sweet spot per recent algo signals; some put in first comment)
- Tone: warmer, faster, visual-first; the slides do most of the work
- Hooks that work: bold claim, beautiful number, before/after, "save this for later"

X (Twitter):
- Slide count: 4-7 slides (longer carousels lose retention here)
- Aspect: 16:9 (X displays carousels horizontally in some clients)
- Caption: 280 chars hard cap — hooks AND payoff must fit
- Hashtag count: 0-2 (more reads as spam on X)
- Tone: punchier, more direct, more contrarian
- Hooks that work: hot take, single number that sounds wrong, hyper-specific question

Rules:

1. Slide 1 (hook) is non-negotiable: it must promise something the
   swiper wants enough to continue. "10 things I learned" is weak. "I
   raised $2M with a 13-slide deck. Here's what was on slide 7." is
   strong. Specific > generic.

2. Each slide pays off the implied promise of the previous slide.
   Don't waste slides; if a slide could be deleted without losing
   the story, delete it.

3. Final slide is either summary (recap value, prompt save/share)
   or cta (ask for the action — book a call, comment, follow). Pick
   one based on the post's primary intent.

4. Cite proof points ONLY from <proof-points>. Never invent traction,
   customer names, or numbers. If a stat slide would benefit from a
   number you don't have, write the slide as a CONCEPT slide
   instead — reframe rather than fabricate.

5. Voice discipline: every word obeys <voice-rules>. Caption follows
   the same rules — no marketing-speak, no hype, no forbidden phrases.

6. Hashtags: relevant + audience-appropriate. NEVER include the kit's
   forbidden phrases as hashtags either. Stay topical.

7. Image hints are CONCRETE. "Numbered chapter slide '02' in giant
   Bebas Neue, gold accent on the chapter number, deep black bg" beats
   "stylized number".

Return ONLY the JSON. No prose preamble."""


def _carousel_text_blob(d: dict[str, Any]) -> str:
    parts: list[str] = []
    parts.append(d.get("title", "") or "")
    parts.append(d.get("hook_promise", "") or "")
    parts.append(d.get("narrative_arc", "") or "")
    for s in (d.get("slides", []) or []):
        if isinstance(s, dict):
            parts.append(join_text_fields(s, (
                "headline", "body", "annotation", "image_hint", "visual_treatment",
            )))
    parts.append(d.get("caption", "") or "")
    parts.append(d.get("first_comment", "") or "")
    parts.append(d.get("engagement_prompt", "") or "")
    parts.extend(d.get("hashtags", []) or [])
    return "\n".join(parts)


async def draft_carousel(
    *,
    business_slug: str,
    audience_slug: str,
    topic: str,
    platform: str = "linkedin",
    slide_count_hint: int | None = None,
    context: str = "",
) -> dict[str, Any]:
    """Draft a social carousel.

    Args:
      business_slug: kit slug
      audience_slug: persona slug from the kit
      topic: what the carousel is about — short phrase
      platform: linkedin (default) | instagram | twitter
      slide_count_hint: optional — override the platform default
      context: free-text additional framing
    """
    kit = load_kit(business_slug)
    audience_md = kit.audience(audience_slug)
    if not audience_md:
        avail = sorted(kit.audiences.keys())
        raise FileNotFoundError(
            f"audience '{audience_slug}' not found in {business_slug} kit. "
            f"Available: {avail}"
        )

    platform = (platform or "linkedin").lower()
    if platform not in ("linkedin", "instagram", "twitter"):
        raise ValueError(f"unsupported platform: {platform}")

    user_prompt = (
        f"{kit.render_for_prompt()}\n\n"
        f"<audience slug=\"{audience_slug}\">\n{audience_md}\n</audience>\n\n"
        f"<platform>{platform}</platform>\n"
        f"<topic>{topic}</topic>\n"
    )
    if slide_count_hint:
        user_prompt += f"<slide-count-hint>{int(slide_count_hint)}</slide-count-hint>\n"
    if context:
        user_prompt += f"\n<additional-context>\n{context[:3000]}\n</additional-context>\n"
    user_prompt += "\nDraft the carousel now. Return JSON only."

    forbidden = kit.brand.get("forbidden_phrases", []) or []
    car_json = await generate_json(
        system=_CAROUSEL_SYSTEM,
        user=user_prompt,
        forbidden=forbidden,
        text_blob_fn=_carousel_text_blob,
        max_tokens=6000,
    )

    title = car_json.get("title") or f"{kit.name} carousel — {topic[:60]}"
    artifact = await create_artifact(
        business_slug=business_slug,
        kind="carousel",
        audience_slug=audience_slug,
        title=title,
        ask=f"{platform}: {topic}",
        content=car_json,
    )
    return artifact
