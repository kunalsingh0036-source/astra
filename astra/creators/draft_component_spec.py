"""
Draft a component-level spec — for handoff to a designer or implementer.

A spec zooms in on one component (hero / feature_card / pricing_table /
testimonial / etc.) and defines: layout, slots, interaction, responsive
behavior, accessibility expectations, image direction, implementation
notes.

This is what you'd attach to a Linear ticket or paste into a Figma
description. Shorter than a brief, more concrete than a page draft.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from astra.creators._shared import generate_json, join_text_fields
from astra.creators.kits import load_kit
from astra.creators.store import create_artifact, get_artifact

logger = logging.getLogger(__name__)


_COMPONENT_SPEC_SYSTEM = """You are Astra's creator sub-agent — component spec writer.

You produce implementation-ready specs for ONE component on a site.
A spec is what a designer / front-end implementer / Cursor-style code
agent reads to build the component without re-reading the brief or
the page draft.

Voice rules in <voice-rules> apply to any user-facing copy you suggest
in the spec (placeholder labels, accessibility text). Forbidden
phrases in <forbidden-phrases> are a hard ban.

Your output is STRICT JSON matching this schema:

{
  "component_type": "<from the standard set: hero | feature_card | feature_grid | pricing_card | pricing_table | testimonial | testimonial_grid | logo_strip | stat_block | cta_block | faq_item | faq_accordion | nav | footer | form | tabs | accordion | dialog_modal | comparison_table | timeline | step_list | code_block | metric_card | quote_block | story_card | callout — or 'custom' with a name>",
  "custom_name": "<only when component_type is 'custom'>",
  "context": "<which page, which section — e.g. 'home > hero', 'pricing > tier card'>",
  "intent": "<the ONE thing this component accomplishes>",

  "structure": {
    "layout": "<2-4 sentences. Concrete description: 'Two-column 60/40 split on desktop. Left: stacked headline + subhead + dual CTAs (primary + ghost). Right: 16:9 product mockup with 8px corner radius and a 32px outer glow in brand-emerald-glow. On tablet, columns stack: text first.'>",
    "slots": [
      {
        "name": "<slot name — kebab-case>",
        "type": "text | rich_text | image | icon | button | input | select | toggle | video | code",
        "purpose": "<what content goes here>",
        "max_chars": <int or null>,
        "voice_register": "<which kit register: smb-customer | investor | etc — only if applicable>",
        "required": <true/false>,
        "default_state": "<placeholder, default value, empty-state behavior>"
      }
    ]
  },

  "interaction": {
    "default_state": "<how it looks when first seen>",
    "hover_state": "<what changes on hover — be specific: 'CTA primary fills with brand-emerald, lifts 2px, casts 0/8/24/0 shadow'>",
    "active_state": "<what changes on click/tap>",
    "focus_state": "<keyboard focus indicator>",
    "scroll_behavior": "<sticky? parallax? reveal-on-scroll? entirely static?>",
    "transitions": "<timing + easing — '180ms cubic-bezier(0.16, 1, 0.3, 1)' for primary; static otherwise>"
  },

  "responsive": {
    "desktop_breakpoint": "<≥1024px behavior>",
    "tablet_breakpoint": "<768-1023px behavior>",
    "mobile_breakpoint": "<<768px behavior>",
    "minimum_supported_width": "<short — e.g. '320px (iPhone SE)'>"
  },

  "accessibility": {
    "semantic_html": "<which element wraps the component — '<section role=\"banner\">', '<article>', '<button>', etc.>",
    "aria_attributes": [
      "<concrete attr — e.g. 'aria-labelledby on the section pointing to the headline id'>"
    ],
    "keyboard_pattern": "<how keyboard users navigate — 'Tab through CTA primary, secondary; Enter/Space activates'>",
    "screen_reader_notes": "<what a screen reader should say in order>",
    "contrast_requirements": "<short — '4.5:1 minimum for body, 3:1 for interactive elements'>",
    "motion_safety": "<how prefers-reduced-motion is honored>"
  },

  "image_direction": {
    "needed": <true/false>,
    "aspect_ratio": "<16:9 | 4:5 | 1:1 | 3:2 | null>",
    "subject": "<one sentence — what the image shows>",
    "treatment": "<one sentence — color treatment, lighting, framing>",
    "anti_patterns": ["<things to avoid>"]
  },

  "implementation_notes": [
    "<concrete note — 'Use CSS grid for the layout, not flexbox; we need 60/40 with explicit column gaps.'>",
    "<concrete note — 'Lazy-load the right-column image; it's below the fold on tablet.'>"
  ],

  "recommended_libraries": [
    {
      "name": "<library/component>",
      "reason": "<why it fits>",
      "alternatives": ["<...>"]
    }
  ],

  "states_and_edge_cases": [
    {
      "case": "<scenario — 'no testimonials yet', 'image fails to load', 'long headline overflow'>",
      "behavior": "<what the component does>"
    }
  ]
}

Rules:

1. Be concrete. "Hover state: CTA fills with brand-emerald, lifts 2px" beats "subtle hover effect". Implementation should be possible without asking follow-up questions.

2. Slots must match the component_type's natural anatomy. A hero has headline + subhead + CTAs + image. A pricing_card has tier_name + price + period + features_list + CTA. Don't over-spec; don't under-spec.

3. Voice register on text slots: pull from the kit's voice.md. SMB-customer slots use the SMB register; investor slots use institutional. Mark each text slot with its register so the page-content draft enforces it.

4. Accessibility is non-negotiable. Every component has semantic_html + aria_attributes + keyboard_pattern + screen_reader_notes + contrast_requirements + motion_safety. No exceptions.

5. recommended_libraries: pick well-known, well-maintained options. For this stack assume: Next.js + React + Tailwind + Framer Motion (for motion) + Radix UI primitives (for a11y-correct interactives). Suggest different ones only if the brief warrants.

6. states_and_edge_cases: think through what could go wrong. Long headlines, missing images, empty data states, network failures.

7. Forbidden phrases: hard ban including in placeholder text, default_state, accessibility labels.

Return ONLY the JSON. No prose preamble."""


def _component_spec_text_blob(d: dict[str, Any]) -> str:
    parts: list[str] = []
    parts.append(d.get("intent", "") or "")
    parts.append(d.get("context", "") or "")
    structure = d.get("structure") or {}
    if isinstance(structure, dict):
        parts.append(structure.get("layout", "") or "")
        for slot in (structure.get("slots") or []):
            if isinstance(slot, dict):
                parts.append(join_text_fields(slot, ("purpose", "default_state")))
    interaction = d.get("interaction") or {}
    if isinstance(interaction, dict):
        parts.append(join_text_fields(interaction, (
            "default_state", "hover_state", "active_state",
            "scroll_behavior",
        )))
    img = d.get("image_direction") or {}
    if isinstance(img, dict):
        parts.append(img.get("subject", "") or "")
        parts.append(img.get("treatment", "") or "")
    parts.extend(d.get("implementation_notes", []) or [])
    return "\n".join(parts)


async def draft_component_spec(
    *,
    business_slug: str,
    component_type: str,
    intent: str,
    page_context: str = "",
    page_content_id: int | None = None,
    site_brief_id: int | None = None,
    audience_slug: str | None = None,
    context: str = "",
) -> dict[str, Any]:
    """Generate a component spec.

    Args:
      business_slug: kit slug
      component_type: e.g. 'hero', 'feature_card', 'pricing_card', 'custom'
      intent: the ONE thing this component accomplishes
      page_context: which page + section ('home > hero')
      page_content_id: optional artifact id of related page_content; the
        component's slot definitions can reference it for live copy
      site_brief_id: optional artifact id; the spec respects the brief's
        style_direction
      audience_slug: optional persona slug; shapes the voice register
      context: free-text additional framing
    """
    kit = load_kit(business_slug)
    audience_md = kit.audience(audience_slug) if audience_slug else ""

    related_blob = ""
    if site_brief_id:
        brief = await get_artifact(int(site_brief_id))
        if brief and brief.get("kind") == "site_brief":
            related_blob += (
                f"<site-brief id=\"{site_brief_id}\">\n"
                f"{json.dumps(brief.get('content'), indent=2)[:6000]}\n"
                f"</site-brief>\n\n"
            )
    if page_content_id:
        page = await get_artifact(int(page_content_id))
        if page and page.get("kind") == "page_content":
            related_blob += (
                f"<page-content id=\"{page_content_id}\">\n"
                f"{json.dumps(page.get('content'), indent=2)[:5000]}\n"
                f"</page-content>\n\n"
            )

    user_prompt = (
        f"{kit.render_for_prompt()}\n\n"
        f"<audience slug=\"{audience_slug or 'unspecified'}\">\n"
        f"{audience_md or '(no specific audience — judge voice register from kit)'}\n"
        f"</audience>\n\n"
        f"<component-type>{component_type}</component-type>\n"
        f"<page-context>{page_context}</page-context>\n"
        f"<intent>{intent}</intent>\n\n"
        f"{related_blob}"
    )
    if context:
        user_prompt += f"<additional-context>\n{context[:3000]}\n</additional-context>\n\n"
    user_prompt += "Draft the component spec now. Return JSON only."

    forbidden = kit.brand.get("forbidden_phrases", []) or []
    spec_json = await generate_json(
        system=_COMPONENT_SPEC_SYSTEM,
        user=user_prompt,
        forbidden=forbidden,
        text_blob_fn=_component_spec_text_blob,
        max_tokens=5000,
    )

    title = f"{kit.name} — {component_type} spec ({page_context or intent[:30]})"
    artifact = await create_artifact(
        business_slug=business_slug,
        kind="component_spec",
        audience_slug=audience_slug,
        title=title[:200],
        ask=intent,
        content=spec_json,
        parent_id=site_brief_id or page_content_id,
    )
    return artifact
