"""
Draft the actual copy + image direction for a single page.

Input: a site_brief artifact + a page slug from its sitemap.
Output: every line of copy that goes on the page — hero, sections,
CTAs, footer — plus image_hint per visual element + meta tags.

This is what gets handed to a developer (or Cursor / Webflow agent)
to actually build the page. Compare to draft_component_spec which
zooms in on a single component.
"""

from __future__ import annotations

import logging
from typing import Any

from astra.creators._shared import generate_json, join_text_fields
from astra.creators.kits import load_kit
from astra.creators.store import create_artifact, get_artifact

logger = logging.getLogger(__name__)


_PAGE_CONTENT_SYSTEM = """You are Astra's creator sub-agent — page content drafter.

You produce every line of copy + every image direction for ONE page
of a site. Input includes: the kit, the audience, the full site brief,
and the page slug to draft. Your output replaces the brief's
content_brief stubs with actual on-page copy.

Voice rules in <voice-rules> are absolute. Forbidden phrases in
<forbidden-phrases> are a hard ban — case-insensitive substring match.

Your output is STRICT JSON matching this schema:

{
  "page_slug": "<the slug from the brief>",
  "title": "<the page title — what shows in browser tab>",
  "meta": {
    "title": "<SEO title — distinct from on-page H1; 50-65 chars>",
    "description": "<meta description — 140-160 chars; written for humans, not keyword-stuffed>",
    "og_title": "<Open Graph title — can equal SEO title>",
    "og_description": "<Open Graph description>",
    "og_image_hint": "<one-line description of an OG image for this page>"
  },
  "sections": [
    {
      "type": "<MUST match the section type from the site brief — same enum>",
      "id": "<anchor id — kebab-case, 2-5 words>",
      "heading": "<H1 for hero, H2 elsewhere; null if section has no heading>",
      "subheading": "<one-line subhead under heading; null if not applicable>",
      "body_md": "<markdown body — actual on-page copy. 1-3 short paragraphs OR replaced by bullets/items below>",
      "bullets": ["<short, parallel bullet text>"],
      "items": [
        {
          "title": "<for cards: feature card title, testimonial author, etc.>",
          "body_md": "<for cards: feature description, testimonial quote, etc.>",
          "image_hint": "<image direction for this item>",
          "meta": "<for testimonials: 'Name, Title, Company'; for pricing: 'price + period'; etc.>"
        }
      ],
      "cta_primary": {
        "label": "<button label — 2-4 words, imperative>",
        "intent": "<what happens — 'open booking modal', 'scroll to pricing', 'submit form'>",
        "destination": "<URL or anchor — '#pricing', '/contact', 'mailto:...'>"
      },
      "cta_secondary": {
        "label": "<...>",
        "intent": "<...>",
        "destination": "<...>"
      },
      "image_hint": "<image direction for the section's hero image, if applicable>",
      "image_aspect": "<16:9 | 4:5 | 1:1 | 3:2 — based on layout intent>"
    }
  ],
  "footer": {
    "tagline": "<short brand line — drawn from the kit>",
    "columns": [
      {
        "heading": "<column heading — 'Product', 'Company', 'Resources', 'Legal'>",
        "links": [
          {"label": "<link text>", "destination": "<URL or anchor>"}
        ]
      }
    ],
    "bottom_line": "<copyright + small print>"
  },
  "global_ctas": {
    "primary": {"label": "<top-right nav CTA>", "destination": "<...>"},
    "secondary": {"label": "<...>", "destination": "<...>"}
  }
}

Rules:

1. Section types MUST match what the brief specifies for this page. Don't invent new sections; if you think the brief is missing a section, say so in body_md as a comment so the human can decide.

2. Voice discipline: every word obeys <voice-rules>. Headlines are short and declarative. Subheads earn their existence. Body paragraphs do work; no filler. Forbidden phrases are a hard ban including in meta tags.

3. Content_brief from the brief is your input. The brief said WHAT this section should accomplish; you say HOW (the actual words).

4. Cite proof points ONLY from <proof-points>. Don't invent traction, customer names, certifications, or numbers. Where the brief calls for proof you don't have, write the section without it OR leave a placeholder like "[traction TBD]" so the reviewer knows.

5. CTAs: every section that drives action gets a cta_primary. Don't drop CTAs into informational sections (faq, narrative, footer); those serve different purposes.

6. items vs bullets: use bullets for short parallel lists (3-6 items, each <80 chars). Use items for cards/grids where each entry has its own structure (title + body + image).

7. image_hint is an INSTRUCTION to a future image-generation step (or a stock-photo lookup). Be concrete: 'Architectural close-up of a glass squash court at dusk, motion-blurred figure mid-shot, gold accent lighting from the upper-right' — not 'a sport photo'.

8. SEO meta should reinforce the value prop, not keyword-stuff. Test: would the meta description make YOU click? If not, rewrite.

9. If the page has only one CTA destination type (e.g. all CTAs go to the same booking modal), still use cta_primary on each relevant section — repetition reinforces conversion. Avoid different secondary CTAs that fragment focus.

Return ONLY the JSON. No prose preamble."""


def _page_text_blob(d: dict[str, Any]) -> str:
    parts: list[str] = []
    parts.append(d.get("title", "") or "")
    meta = d.get("meta") or {}
    if isinstance(meta, dict):
        parts.append(join_text_fields(meta, (
            "title", "description", "og_title", "og_description", "og_image_hint",
        )))
    for s in (d.get("sections", []) or []):
        if isinstance(s, dict):
            parts.append(join_text_fields(s, (
                "heading", "subheading", "body_md", "image_hint",
            )))
            parts.extend(str(b) for b in (s.get("bullets") or []))
            for item in (s.get("items") or []):
                if isinstance(item, dict):
                    parts.append(join_text_fields(item, (
                        "title", "body_md", "image_hint", "meta",
                    )))
            for cta_key in ("cta_primary", "cta_secondary"):
                cta = s.get(cta_key) or {}
                if isinstance(cta, dict):
                    parts.append(join_text_fields(cta, ("label", "intent")))
    footer = d.get("footer") or {}
    if isinstance(footer, dict):
        parts.append(footer.get("tagline", "") or "")
        parts.append(footer.get("bottom_line", "") or "")
        for col in (footer.get("columns") or []):
            if isinstance(col, dict):
                parts.append(col.get("heading", "") or "")
                for link in (col.get("links") or []):
                    if isinstance(link, dict):
                        parts.append(link.get("label", "") or "")
    return "\n".join(parts)


async def draft_page_content(
    *,
    site_brief_id: int,
    page_slug: str,
    context: str = "",
) -> dict[str, Any]:
    """Draft all on-page copy for one page of a site.

    Args:
      site_brief_id: artifact id of a kind="site_brief"
      page_slug: which page from the brief's sitemap to draft
      context: optional additional framing
    """
    brief = await get_artifact(site_brief_id)
    if not brief:
        raise FileNotFoundError(f"site_brief #{site_brief_id} not found")
    if brief.get("kind") != "site_brief":
        raise ValueError(
            f"artifact #{site_brief_id} is kind={brief['kind']!r}, not 'site_brief'"
        )

    brief_content = brief.get("content") or {}
    sitemap = brief_content.get("sitemap", []) or []
    target_page = next((p for p in sitemap if p.get("slug") == page_slug), None)
    if not target_page:
        slugs = [p.get("slug") for p in sitemap]
        raise FileNotFoundError(
            f"page_slug '{page_slug}' not found in brief sitemap. "
            f"Available: {slugs}"
        )

    business_slug = brief["business_slug"]
    audience_slug = brief.get("audience_slug")
    kit = load_kit(business_slug)
    audience_md = kit.audience(audience_slug) if audience_slug else ""

    import json as _json
    user_prompt = (
        f"{kit.render_for_prompt()}\n\n"
        f"<audience slug=\"{audience_slug or 'unspecified'}\">\n"
        f"{audience_md or '(no audience persona on file)'}\n"
        f"</audience>\n\n"
        f"<site-brief id=\"{site_brief_id}\">\n"
        f"{_json.dumps(brief_content, indent=2)[:14000]}\n"
        f"</site-brief>\n\n"
        f"<target-page-slug>{page_slug}</target-page-slug>\n"
        f"<target-page-from-brief>\n{_json.dumps(target_page, indent=2)}\n"
        f"</target-page-from-brief>\n\n"
    )
    if context:
        user_prompt += f"<additional-context>\n{context[:3000]}\n</additional-context>\n\n"
    user_prompt += "Draft the page content now. Return JSON only."

    forbidden = kit.brand.get("forbidden_phrases", []) or []
    page_json = await generate_json(
        system=_PAGE_CONTENT_SYSTEM,
        user=user_prompt,
        forbidden=forbidden,
        text_blob_fn=_page_text_blob,
        max_tokens=8000,
    )

    title = f"{kit.name} site — {target_page.get('title', page_slug)}"
    artifact = await create_artifact(
        business_slug=business_slug,
        kind="page_content",
        audience_slug=audience_slug,
        title=title,
        ask=f"page: {page_slug}",
        content=page_json,
        parent_id=site_brief_id,
    )
    return artifact
