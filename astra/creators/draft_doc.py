"""
Draft a long-form document — proposals, briefs, MoUs, white papers.

A doc is multi-page, reads as institutional, and stands on its own
without slides or accompanying narration. Typical use: investor data
room contents, partnership MoU drafts, formal briefs to government /
enterprise procurement, RFP responses.
"""

from __future__ import annotations

import logging
from typing import Any

from astra.creators._shared import generate_json, join_text_fields
from astra.creators.kits import load_kit
from astra.creators.store import create_artifact

logger = logging.getLogger(__name__)


_DOC_SYSTEM = """You are Astra's creator sub-agent — the long-form document role.

You produce institutional-grade documents: proposals, briefs, MoU
drafts, white papers, RFP responses. The document reads as though the
recipient's general counsel will skim it for tone and clarity. It must
be unhedged, specific, and self-contained.

Voice rules in <voice-rules> are absolute. Forbidden phrases in
<forbidden-phrases> are a hard ban — case-insensitive substring match.

Your output is STRICT JSON matching this schema:

{
  "title": "<document title — short, declarative>",
  "subtitle": "<one-line subtitle / context>",
  "doc_type": "proposal" | "brief" | "mou_draft" | "white_paper" | "rfp_response",
  "executive_summary": "<2-4 paragraphs. The standalone TL;DR. If the recipient reads ONLY this, they should be fully informed.>",
  "sections": [
    {
      "heading": "<section heading — 3-7 words, title case>",
      "body_md": "<markdown body — 2-6 paragraphs. May include sub-headings (### in markdown), tables, and bullet lists where they earn their place. Never bullets-as-decoration.>"
    }
  ],
  "appendix": [
    {
      "heading": "<appendix heading — 'Specifications', 'Pricing', 'Compliance', etc.>",
      "body_md": "<markdown — typically reference tables, certifications, contact details>"
    }
  ],
  "cta": {
    "headline": "<the explicit ask — what we want the recipient to do>",
    "detail": "<one paragraph — exact next-step including timeline and contact>"
  },
  "footer_note": "<one-line — confidentiality, version, document-id, etc.>"
}

Rules:

1. Length: 4-9 sections (NOT counting executive_summary, appendix, or cta). Each section is 2-6 paragraphs of meaningful content. Documents shorter than 4 sections should be a one-pager instead; longer than 9 should be split.

2. Executive summary stands alone. A reader who doesn't continue past it should be fully informed of the proposal/brief/argument.

3. Cite proof points ONLY from <proof-points>. No invented traction, customer names, certifications, or numbers. Where a number would help but isn't in the kit, write the section without it OR mark `[TBD: <description>]` so the human reviewer can fill it.

4. Voice discipline: every word obeys <voice-rules>. Active voice. Specific not abstract. The institutional register (not the SMB-customer register) is default for docs unless <audience> says otherwise.

5. Appendix is for facts that interrupt narrative flow — tables, certifications, contact details, pricing schedules. 0-3 appendices typically; rare to need more.

6. CTA is explicit. "Sign and return by [date]". "Schedule the kickoff call by [date]". "Submit the PO referencing [ref]". Never vague.

7. Forbidden phrases: hard ban.

Return ONLY the JSON. No prose preamble. No markdown fences."""


def _doc_text_blob(d: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("title", "subtitle", "executive_summary", "footer_note"):
        v = d.get(key)
        if isinstance(v, str):
            parts.append(v)
    for key in ("sections", "appendix"):
        for s in (d.get(key, []) or []):
            if isinstance(s, dict):
                parts.append(join_text_fields(s, ("heading", "body_md")))
    cta = d.get("cta") or {}
    if isinstance(cta, dict):
        parts.append(str(cta.get("headline", "")))
        parts.append(str(cta.get("detail", "")))
    return "\n".join(parts)


async def draft_doc(
    *,
    business_slug: str,
    audience_slug: str,
    ask: str,
    doc_type: str = "proposal",
    context: str = "",
) -> dict[str, Any]:
    """Generate a doc and persist it. Returns the saved artifact dict.

    `doc_type` is a hint to the model — "proposal", "brief",
    "mou_draft", "white_paper", "rfp_response". The model echoes
    it in the JSON; the renderer can use it for cosmetic differences
    (e.g. an MoU draft gets a different cover treatment).
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
        f"<doc-type>{doc_type}</doc-type>\n"
        f"<ask>{ask}</ask>\n\n"
    )
    if context:
        user_prompt += f"<additional-context>\n{context[:6000]}\n</additional-context>\n\n"
    user_prompt += "Draft the document now. Return JSON only."

    forbidden = kit.brand.get("forbidden_phrases", []) or []
    doc_json = await generate_json(
        system=_DOC_SYSTEM,
        user=user_prompt,
        forbidden=forbidden,
        text_blob_fn=_doc_text_blob,
    )

    title = doc_json.get("title") or f"{kit.name} — {ask[:60]}"
    artifact = await create_artifact(
        business_slug=business_slug,
        kind="doc",
        audience_slug=audience_slug,
        title=title,
        ask=ask,
        content=doc_json,
    )
    return artifact
