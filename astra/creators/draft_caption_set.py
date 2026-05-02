"""
Draft a SET of caption variants for the same artifact / image / topic.

Used for A/B testing or for picking the best of N. Each variant
uses a different angle (hook style, length, register) but stays in
the kit's voice.
"""

from __future__ import annotations

import logging
from typing import Any

from astra.creators._shared import generate_json, join_text_fields
from astra.creators.kits import load_kit
from astra.creators.store import create_artifact

logger = logging.getLogger(__name__)


_CAPTION_SET_SYSTEM = """You are Astra's creator sub-agent — caption-variant drafter.

You produce N distinct caption variants for the same underlying topic
or image. Each variant is voice-compliant; the variants differ in
HOOK STYLE / LENGTH / REGISTER, not in factual content.

Voice rules in <voice-rules> are absolute. Forbidden phrases in
<forbidden-phrases> are a hard ban — case-insensitive substring match.

Your output is STRICT JSON matching this schema:

{
  "topic": "<what the captions are about>",
  "platform": "linkedin" | "instagram" | "twitter" | "facebook",
  "subject_summary": "<one sentence — what the founder is sharing>",
  "variants": [
    {
      "label": "<a short tag for this variant — 'contrarian-hook', 'data-led', 'story-led', 'short-and-direct', 'longer-narrative', 'question-first', 'before/after'>",
      "hook_style": "<short — the hook technique used>",
      "length_target": "short" | "medium" | "long",
      "body": "<the caption body. Length appropriate to length_target.>",
      "first_line": "<the EXACT first line — what shows above the 'see more' fold>",
      "engagement_prompt": "<optional final question to drive replies>",
      "predicted_strength": "<one short phrase — when THIS variant would beat the others. e.g. 'cold audience that hasn't seen the founder before', 'warm audience already converted on the thesis'>"
    }
  ]
}

Length conventions:
  short: 1-2 sentences (good for X — under 280 chars)
  medium: 3-6 sentences (good for LinkedIn previews — 600-900 chars)
  long: 7+ sentences (good for full LinkedIn / Instagram caption)

Rules:

1. Variants must be GENUINELY DIFFERENT. If two variants would land
   identically with the same audience, drop one and add another that
   tests a different hook style.

2. Every variant must work as a standalone post. No "see image" or
   "as I shared earlier" cross-references unless the founder
   explicitly told you the post is a follow-up.

3. The "predicted_strength" is your honest call about when each
   variant beats the others. The founder uses this to pick. Don't
   hedge: "this works for everyone" is useless.

4. Voice discipline: every variant obeys <voice-rules>. The variants
   differ in technique, not in voice register (unless the audience
   persona allows multiple registers).

5. Forbidden phrases: hard ban across all variants.

6. Hashtags / mentions: NOT included here — those go in
   draft_hashtag_set. Captions are pure body text.

7. Cite proof points ONLY from <proof-points>. No invented traction.

8. Recommended variant count: 3-5. Fewer than 3 doesn't give the
   founder a real choice; more than 5 dilutes quality.

Return ONLY the JSON. No prose preamble."""


def _caption_set_text_blob(d: dict[str, Any]) -> str:
    parts: list[str] = []
    parts.append(d.get("topic", "") or "")
    parts.append(d.get("subject_summary", "") or "")
    for v in (d.get("variants", []) or []):
        if isinstance(v, dict):
            parts.append(join_text_fields(v, (
                "body", "first_line", "engagement_prompt", "hook_style",
            )))
    return "\n".join(parts)


async def draft_caption_set(
    *,
    business_slug: str,
    audience_slug: str,
    topic: str,
    platform: str = "linkedin",
    variant_count: int = 4,
    context: str = "",
) -> dict[str, Any]:
    """Draft a set of caption variants.

    Args:
      business_slug: kit slug
      audience_slug: persona slug
      topic: subject of the captions
      platform: linkedin | instagram | twitter | facebook
      variant_count: 3-5 (clamped)
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
    variant_count = max(3, min(5, int(variant_count)))

    user_prompt = (
        f"{kit.render_for_prompt()}\n\n"
        f"<audience slug=\"{audience_slug}\">\n{audience_md}\n</audience>\n\n"
        f"<platform>{platform}</platform>\n"
        f"<topic>{topic}</topic>\n"
        f"<variant-count>{variant_count}</variant-count>\n"
    )
    if context:
        user_prompt += f"\n<additional-context>\n{context[:2500]}\n</additional-context>\n"
    user_prompt += "\nDraft the caption set now. Return JSON only."

    forbidden = kit.brand.get("forbidden_phrases", []) or []
    cs_json = await generate_json(
        system=_CAPTION_SET_SYSTEM,
        user=user_prompt,
        forbidden=forbidden,
        text_blob_fn=_caption_set_text_blob,
        max_tokens=4000,
    )

    title = f"{kit.name} captions ({variant_count}× {platform}) — {topic[:50]}"
    artifact = await create_artifact(
        business_slug=business_slug,
        kind="caption_set",
        audience_slug=audience_slug,
        title=title,
        ask=topic,
        content=cs_json,
    )
    return artifact
