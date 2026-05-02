"""
Draft a site brief — sitemap + page IA + style direction + functionality.

The brief is the bridge between brand kit and shipped site. It says:
"For this business, given this audience and these goals, here's the
sitemap, the per-page IA, the style direction extending the brand kit
into web specifics, the required functionality, and what to borrow
from each reference analysis."

A brief is what you'd hand to a designer. A page-content draft (next
tool) is what you'd hand to a developer.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from astra.creators._shared import generate_json, join_text_fields
from astra.creators.kits import load_kit
from astra.creators.store import create_artifact, get_artifact

logger = logging.getLogger(__name__)


_BRIEF_SYSTEM = """You are Astra's creator sub-agent — site brief drafter.

You produce site briefs for one of Kunal's portfolio companies (or a
client kit drafted via Top Studios). The brief defines a complete
site at the IA + style + functionality level. Page-by-page copy comes
later via draft_page_content; component-level specs via draft_component_spec.

Voice rules in <voice-rules> are absolute. Forbidden phrases in
<forbidden-phrases> are a hard ban — case-insensitive substring match.

Your output is STRICT JSON matching this schema:

{
  "title": "<site title — short, declarative>",
  "subtitle": "<positioning sentence>",
  "site_kind": "marketing_site" | "saas_app" | "portfolio" | "ecommerce" | "documentation" | "blog" | "campaign_microsite",
  "primary_goal": "<the ONE thing this site optimizes for — a single sentence>",
  "secondary_goals": [
    "<other things this site does, but not at the cost of primary_goal>"
  ],
  "sitemap": [
    {
      "slug": "<url-slug — kebab-case>",
      "title": "<nav label / page title>",
      "intent": "<what this page does for the visitor>",
      "kind": "home" | "product" | "pricing" | "about" | "case_study" | "blog_index" | "blog_post" | "contact" | "legal" | "documentation" | "demo" | "other",
      "sections": [
        {
          "type": "hero" | "value_prop" | "features" | "social_proof" | "pricing" | "faq" | "cta_block" | "footer" | "testimonials" | "process" | "team" | "stats" | "logos" | "narrative" | "demo" | "comparison",
          "intent": "<what this section accomplishes>",
          "components": ["<component-tag>", "..."],
          "content_brief": "<2-3 sentences telling draft_page_content what to write here. Specific. References proof points by what they prove, not by literal numbers (those come from the kit at draft time).>"
        }
      ]
    }
  ],
  "style_direction": {
    "tone": "<institutional | editorial | playful | raw | technical | luxury — pick from voice register>",
    "density": "minimal" | "standard" | "dense",
    "motion": "<one paragraph: when motion is used, when it isn't — typed to brand register>",
    "navigation_grammar": "<one paragraph: how primary nav works, what's surfaced, sticky vs scroll-revealing, mobile pattern>",
    "imagery_direction": "<extends brand.imagery into web specifics — hero treatment, photo vs illustration mix, aspect ratios>",
    "extra_palette_notes": "<any web-only color extensions beyond brand.yml — e.g. semantic-success, semantic-warning, hover states>"
  },
  "functionality": [
    {
      "name": "<feature name>",
      "scope": "<what it does>",
      "complexity": "low" | "medium" | "high",
      "third_party_recommendation": "<service / library if relevant — e.g. 'Cal.com for booking', 'Plausible for analytics', or 'custom'>"
    }
  ],
  "reference_notes": [
    {
      "ref_id": "<analysis artifact id, if cited>",
      "what_to_borrow": "<concrete pattern from that analysis to adopt>",
      "what_to_skip": "<what NOT to copy from it>"
    }
  ],
  "performance_budget": {
    "lcp_target_seconds": <number>,
    "image_optimization": "<short — formats, lazy loading policy>",
    "third_party_budget": "<short — how many third-party scripts at most>"
  },
  "accessibility_baseline": "<one paragraph — WCAG level, keyboard, contrast, focus, semantic HTML expectations>"
}

Rules:

1. Sitemap discipline: 4-9 pages for a marketing site; 2-4 pages for a campaign microsite; can be more for documentation. Resist the "more pages = more value" instinct — every page is a maintenance cost.

2. Section types: pick the closest from the enum. Each section has a clear job. No section should be there for visual rhythm only — every section earns its place by serving the page's intent.

3. content_brief per section: this becomes input to draft_page_content. Be specific about what to say. "Trust signals — three logos of named clients with their permission status flagged" beats "social proof".

4. style_direction must extend the brand kit, not replace it. Brand colors and fonts come from the kit. Add motion/density/navigation grammar/imagery direction here.

5. Reference notes: only cite reference analyses the user provided in <references>. Do not invent reference URLs. If no references provided, skip reference_notes.

6. Voice discipline: every word obeys <voice-rules>. The site brief reads as a designer's brief, not as a marketing pitch.

7. Forbidden phrases: hard ban.

Return ONLY the JSON. No prose preamble."""


def _brief_text_blob(d: dict[str, Any]) -> str:
    parts: list[str] = []
    parts.append(d.get("title", "") or "")
    parts.append(d.get("subtitle", "") or "")
    parts.append(d.get("primary_goal", "") or "")
    parts.extend(d.get("secondary_goals", []) or [])
    for p in (d.get("sitemap", []) or []):
        if isinstance(p, dict):
            parts.append(join_text_fields(p, ("title", "intent")))
            for s in (p.get("sections", []) or []):
                if isinstance(s, dict):
                    parts.append(join_text_fields(s, ("intent", "content_brief")))
    sd = d.get("style_direction") or {}
    if isinstance(sd, dict):
        parts.append(join_text_fields(sd, (
            "motion", "navigation_grammar", "imagery_direction",
            "extra_palette_notes",
        )))
    for f in (d.get("functionality", []) or []):
        if isinstance(f, dict):
            parts.append(join_text_fields(f, ("name", "scope")))
    return "\n".join(parts)


async def draft_site_brief(
    *,
    business_slug: str,
    audience_slug: str,
    primary_goal: str,
    site_kind: str = "marketing_site",
    reference_analysis_ids: list[int] | None = None,
    context: str = "",
) -> dict[str, Any]:
    """Generate a site brief and persist it.

    Args:
      business_slug: kit slug
      audience_slug: persona slug from the kit
      primary_goal: the ONE thing the site optimizes for
      site_kind: shape hint — marketing_site, saas_app, portfolio,
        ecommerce, documentation, blog, campaign_microsite
      reference_analysis_ids: list of site_analysis artifact ids to
        cite as borrow-from references. Each must already exist via
        analyze_reference_site.
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

    # Resolve references
    refs_blob = ""
    if reference_analysis_ids:
        ref_chunks: list[str] = []
        for rid in reference_analysis_ids:
            ref = await get_artifact(int(rid))
            if not ref or ref.get("kind") != "site_analysis":
                raise FileNotFoundError(
                    f"reference id #{rid} is not a site_analysis artifact"
                )
            ref_chunks.append(
                f"<reference id=\"{rid}\" url=\"{(ref.get('content') or {}).get('url','')}\">\n"
                f"{json.dumps(ref.get('content'), indent=2)[:6000]}\n"
                f"</reference>"
            )
        refs_blob = "<references>\n" + "\n\n".join(ref_chunks) + "\n</references>\n\n"

    user_prompt = (
        f"{kit.render_for_prompt()}\n\n"
        f"<audience slug=\"{audience_slug}\">\n{audience_md}\n</audience>\n\n"
        f"<site-kind>{site_kind}</site-kind>\n"
        f"<primary-goal>{primary_goal}</primary-goal>\n\n"
        f"{refs_blob}"
    )
    if context:
        user_prompt += f"<additional-context>\n{context[:4000]}\n</additional-context>\n\n"
    user_prompt += "Draft the site brief now. Return JSON only."

    forbidden = kit.brand.get("forbidden_phrases", []) or []
    brief_json = await generate_json(
        system=_BRIEF_SYSTEM,
        user=user_prompt,
        forbidden=forbidden,
        text_blob_fn=_brief_text_blob,
        max_tokens=8000,
    )

    title = brief_json.get("title") or f"{kit.name} site brief"
    artifact = await create_artifact(
        business_slug=business_slug,
        kind="site_brief",
        audience_slug=audience_slug,
        title=title,
        ask=primary_goal,
        content=brief_json,
    )
    return artifact
