"""
Draft a one-page sales sheet / fact sheet.

A one-pager is denser than a deck slide and shorter than a doc.
Typical use: leave-behind after a sponsor meeting, a product spec
sheet, an executive summary that can be emailed standalone. Always
fits on a single A4 page when rendered.
"""

from __future__ import annotations

import logging
from typing import Any

from astra.creators._shared import generate_json, join_text_fields
from astra.creators.kits import load_kit
from astra.creators.store import create_artifact

logger = logging.getLogger(__name__)


_ONE_PAGER_SYSTEM = """You are Astra's creator sub-agent — the one-pager-drafting role.

A one-pager is a single A4 page that stands on its own. It must
deliver the full pitch in one density-tuned page. Recipients will
print it, forward it, or read it once at speed. Density matters,
but so does breathing room — never crammed.

Voice rules in <voice-rules> are absolute. Forbidden phrases in
<forbidden-phrases> are a hard ban — case-insensitive substring match.

Your output is STRICT JSON matching this schema:

{
  "title": "<top-of-page title — short, declarative>",
  "subtitle": "<one-line tagline or value-prop>",
  "hero_stat": {
    "value": "<the single number or short phrase that anchors the page — e.g. '220 GSM' or '₹15,000' or '520+ tools'>",
    "label": "<short context — e.g. 'combed ring-spun cotton' or 'website + AI agent' or 'tools, growing'>"
  },
  "lead": "<one-paragraph intro — 2-4 sentences. The TL;DR for someone who reads only this paragraph.>",
  "sections": [
    {
      "heading": "<section heading — 2-5 words>",
      "body_md": "<markdown body — 1-3 short paragraphs OR 3-5 bullets in markdown form>"
    }
  ],
  "proof": [
    "<one-line proof point — credential, traction, capability — sourced from <proof-points>>"
  ],
  "cta": {
    "headline": "<short call-to-action — imperative voice>",
    "detail": "<one-line follow-up — what to do, who to contact>"
  }
}

Rules:

1. Length: 3-5 sections. Each section heading is 2-5 words; body is 1-3 short paragraphs OR 3-5 markdown bullets. The whole thing must fit on one A4 page when rendered — be ruthless.

2. Hero stat is the anchor. Pick the one number that, by itself, justifies the conversation. For HelmTech investor: "$2M pre-seed". For Apex bulk-buyer: "220 GSM". For BAY sponsor: "200+ athletes". Always grounded in <proof-points>.

3. Lead paragraph carries the TL;DR. If the recipient reads ONLY the lead, they should know what this is and why it matters.

4. Proof bullets are facts only — credentials, certifications, traction numbers, named clients (with permission). All from <proof-points>; never invented.

5. CTA is explicit and low-friction. Single ask. No "circle back", no "let's chat" — concrete next step.

6. Voice discipline: every word obeys <voice-rules>. Active voice. No hedging. No hype.

7. Forbidden phrases: hard ban. The check is case-insensitive substring match.

Return ONLY the JSON. No prose preamble. No markdown fences."""


def _one_pager_text_blob(d: dict[str, Any]) -> str:
    parts: list[str] = []
    parts.append(d.get("title", "") or "")
    parts.append(d.get("subtitle", "") or "")
    parts.append(d.get("lead", "") or "")
    hs = d.get("hero_stat") or {}
    if isinstance(hs, dict):
        parts.append(str(hs.get("value", "")))
        parts.append(str(hs.get("label", "")))
    for s in (d.get("sections", []) or []):
        if isinstance(s, dict):
            parts.append(join_text_fields(s, ("heading", "body_md")))
    parts.extend(str(p) for p in (d.get("proof", []) or []))
    cta = d.get("cta") or {}
    if isinstance(cta, dict):
        parts.append(str(cta.get("headline", "")))
        parts.append(str(cta.get("detail", "")))
    return "\n".join(parts)


async def draft_one_pager(
    *,
    business_slug: str,
    audience_slug: str,
    ask: str,
    context: str = "",
) -> dict[str, Any]:
    """Generate a one-pager and persist it. Returns the saved artifact dict."""
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
    user_prompt += "Draft the one-pager now. Return JSON only."

    forbidden = kit.brand.get("forbidden_phrases", []) or []
    page_json = await generate_json(
        system=_ONE_PAGER_SYSTEM,
        user=user_prompt,
        forbidden=forbidden,
        text_blob_fn=_one_pager_text_blob,
    )

    title = page_json.get("title") or f"{kit.name} — {ask[:60]}"
    artifact = await create_artifact(
        business_slug=business_slug,
        kind="one_pager",
        audience_slug=audience_slug,
        title=title,
        ask=ask,
        content=page_json,
    )
    return artifact
