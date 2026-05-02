"""
Draft an audience-tuned set of hashtags.

Hashtags vary by platform (X uses ~0-2, LinkedIn 3-5, Instagram 8-15
sweet spot per recent algo signals). This tool produces three layers
per platform:

  - Brand: hashtags tied to the company (#HelmTech, #BuildingIndiasAIBackbone)
  - Topical: hashtags tied to THIS post's subject (#sovereign-AI, #MCP)
  - Reach: broader hashtags for discovery (#India tech / #AI / etc.)

Output is structured so the founder can copy the right slice depending
on platform.
"""

from __future__ import annotations

import logging
from typing import Any

from astra.creators._shared import generate_json, join_text_fields
from astra.creators.kits import load_kit
from astra.creators.store import create_artifact

logger = logging.getLogger(__name__)


_HASHTAG_SET_SYSTEM = """You are Astra's creator sub-agent — hashtag-set drafter.

You produce a structured hashtag set tuned to the audience and
platform. Hashtags layer by intent: BRAND (about the company),
TOPICAL (about THIS post), REACH (broader categorization).

Voice rules in <voice-rules> apply: the brand-side hashtags must not
contain forbidden phrases. Forbidden phrases in <forbidden-phrases>
are a hard ban.

Your output is STRICT JSON matching this schema:

{
  "topic": "<what the post is about>",
  "primary_platform": "linkedin" | "instagram" | "twitter" | "facebook",
  "brand_tags": [
    "<hashtag without # — tied to the company / its products / its slogans>"
  ],
  "topical_tags": [
    "<hashtag without # — tied to THIS post's subject>"
  ],
  "reach_tags": [
    "<hashtag without # — broader categorization for discovery>"
  ],
  "platform_recommendations": {
    "linkedin": {
      "use": ["<5-7 hashtags from above>"],
      "rationale": "<one short sentence>"
    },
    "instagram": {
      "use": ["<8-15 hashtags from above>"],
      "rationale": "<one short sentence>"
    },
    "twitter": {
      "use": ["<0-2 hashtags from above>"],
      "rationale": "<one short sentence>"
    }
  },
  "avoid": [
    "<a hashtag the founder might be tempted to use but shouldn't, with one-line rationale>"
  ],
  "notes": "<2-3 sentences — broader notes about the hashtag landscape for this topic. Are tags trending? Saturated? Brand-tag worth establishing? etc.>"
}

Rules:

1. Brand tags: pull from the kit. If the kit has clear brand handles
   ("HelmTech", "BuildingIndiasAIBackbone", "ApexHuman"), use those.
   Don't invent brand handles unless they fit the kit obviously.

2. Topical tags: SPECIFIC to this post. "AI" is too broad to be
   topical; "sovereignAI" or "MCPfabric" or "IndianFintech" is
   topical.

3. Reach tags: broader, but still relevant to the audience the kit
   targets. "marketing" is too broad; "B2BSaaS" or "IndianStartups"
   is reach-appropriate.

4. Platform recommendations: pick from the THREE arrays above. Don't
   add hashtags to platform_recommendations that aren't in
   brand_tags / topical_tags / reach_tags.

5. Avoid list: think through what the founder might be TEMPTED to
   tag but shouldn't — over-broad ("startup", "tech"), wrong-audience
   ("entrepreneur" for an institutional pitch), banned by the kit.

6. Forbidden phrases: hashtags must not encode forbidden phrases.
   E.g. if "world-class" is forbidden, "WorldClass" as a hashtag is
   forbidden.

7. Hashtag style: CamelCase for multi-word ("BuildingIndiasAIBackbone"),
   not snake_case or kebab-case (those don't work as hashtags).

8. Count caps: brand_tags 2-4, topical_tags 5-12, reach_tags 4-8.

Return ONLY the JSON. No prose preamble."""


def _hashtag_text_blob(d: dict[str, Any]) -> str:
    parts: list[str] = []
    parts.append(d.get("topic", "") or "")
    parts.append(d.get("notes", "") or "")
    parts.extend(d.get("brand_tags", []) or [])
    parts.extend(d.get("topical_tags", []) or [])
    parts.extend(d.get("reach_tags", []) or [])
    for plat in (d.get("platform_recommendations") or {}).values():
        if isinstance(plat, dict):
            parts.append(plat.get("rationale", "") or "")
    return "\n".join(parts)


async def draft_hashtag_set(
    *,
    business_slug: str,
    topic: str,
    primary_platform: str = "linkedin",
    audience_slug: str | None = None,
    context: str = "",
) -> dict[str, Any]:
    """Draft a structured hashtag set.

    Args:
      business_slug: kit slug
      topic: what the post is about
      primary_platform: linkedin (default) | instagram | twitter | facebook
      audience_slug: optional — shapes which platforms get prioritized
      context: optional additional framing
    """
    kit = load_kit(business_slug)
    audience_md = (kit.audience(audience_slug) or "") if audience_slug else ""

    user_prompt = (
        f"{kit.render_for_prompt()}\n\n"
        f"<audience slug=\"{audience_slug or 'unspecified'}\">\n"
        f"{audience_md or '(no specific audience — judge from kit)'}\n"
        f"</audience>\n\n"
        f"<primary-platform>{primary_platform}</primary-platform>\n"
        f"<topic>{topic}</topic>\n"
    )
    if context:
        user_prompt += f"\n<additional-context>\n{context[:2000]}\n</additional-context>\n"
    user_prompt += "\nDraft the hashtag set now. Return JSON only."

    forbidden = kit.brand.get("forbidden_phrases", []) or []
    hs_json = await generate_json(
        system=_HASHTAG_SET_SYSTEM,
        user=user_prompt,
        forbidden=forbidden,
        text_blob_fn=_hashtag_text_blob,
        max_tokens=2500,
    )

    title = f"{kit.name} hashtags — {topic[:60]}"
    artifact = await create_artifact(
        business_slug=business_slug,
        kind="hashtag_set",
        audience_slug=audience_slug,
        title=title,
        ask=topic,
        content=hs_json,
    )
    return artifact
