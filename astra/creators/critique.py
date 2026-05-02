"""
Critique a creator artifact — voice / audience / proof / structure.

The critique pass uses Haiku (cheaper, faster) since the deep work
already happened during Sonnet drafting. Critique reads the same
kit + audience + artifact bundle and returns structured feedback that
either confirms quality or flags specific issues with suggested fixes.

Use cases:
- Pre-flight check before sending an artifact to its real audience
- Quality-gating in batch jobs (generate 5 variants, critique each,
  pick the best)
- Self-improvement loops (feed critique back to the drafter for v2)
"""

from __future__ import annotations

import logging
from typing import Any

from astra.creators._shared import (
    CRITIQUE_MODEL,
    generate_json,
    join_text_fields,
)
from astra.creators.kits import load_kit
from astra.creators.store import create_artifact, get_artifact

logger = logging.getLogger(__name__)


_CRITIQUE_SYSTEM = """You are Astra's creator-critic — second-pass quality reviewer.

Given a kit (brand, voice, thesis, proof-points), an audience persona,
and an artifact (deck/doc/one-pager), you produce structured critique
that the founder uses to decide ship-or-revise.

Be honest. The founder reads this to catch things; effusive praise is
useless to them. Score harshly when warranted. The artifact will not
be auto-fixed — the founder uses your critique to decide whether to
regenerate.

Output STRICT JSON matching this schema:

{
  "overall_score": <integer 0-100>,
  "verdict": "ship" | "revise" | "rewrite",
  "summary": "<1-2 sentences — what's working, what's the biggest issue>",
  "voice_compliance": {
    "score": <0-100>,
    "notes": "<one paragraph — does it sound like the kit's voice? hedging? hype? wrong register?>",
    "issues": [
      {"location": "<slide N / section heading / etc.>", "issue": "<what's off>", "fix": "<concrete suggestion>"}
    ]
  },
  "audience_fit": {
    "score": <0-100>,
    "notes": "<one paragraph — does it open the way THIS audience wants to be opened? does it address their objections? lead with what they care about?>",
    "issues": [
      {"location": "<...>", "issue": "<...>", "fix": "<...>"}
    ]
  },
  "factual_grounding": {
    "score": <0-100>,
    "notes": "<one paragraph — every number/claim/credential matches <proof-points>? any invented traction or customer names?>",
    "issues": [
      {"location": "<...>", "issue": "<...>", "fix": "<...>"}
    ]
  },
  "structure_and_flow": {
    "score": <0-100>,
    "notes": "<one paragraph — does the argument build? are there orphan slides/sections? is the ASK explicit?>",
    "issues": [
      {"location": "<...>", "issue": "<...>", "fix": "<...>"}
    ]
  },
  "top_three_fixes": [
    "<single most important fix>",
    "<second most important>",
    "<third>"
  ]
}

Verdict thresholds (advisory):
  overall ≥ 85 → ship
  60-84      → revise (specific issues; one regeneration likely fixes)
  < 60       → rewrite (structural problems; start over with new approach)

Rules:
1. Score harshly. Most artifacts have real problems; saying "looks great" wastes the founder's time.
2. Issue locations must be specific — "slide 3", "section heading 'Why Now'", "the lead paragraph". Not vague.
3. Fixes must be concrete — "drop the 'world-class' phrase, replace with the specific GSM number". Not "improve clarity".
4. top_three_fixes is the founder's punch list. The 3 things that will most improve the artifact if addressed.
5. If voice_compliance issues exist, that's usually verdict=revise minimum. Voice problems compound.

Return ONLY the JSON."""


def _critique_text_blob(c: dict[str, Any]) -> str:
    """Critique itself doesn't need scanning for forbidden phrases —
    it's about the artifact, not voice-compliant in itself. Return ""
    so the regeneration loop never triggers."""
    return ""


def _summarize_artifact(artifact: dict[str, Any]) -> str:
    """Render an artifact's content as text the critic can read.

    The shape varies (deck has slides, doc has sections, one_pager has
    sections + cta). We flatten everything to a labelled-block format
    that the critic reads as if it were the rendered artifact.
    """
    kind = artifact.get("kind", "")
    content = artifact.get("content") or {}
    parts: list[str] = [
        f"<artifact-id>{artifact.get('id')}</artifact-id>",
        f"<kind>{kind}</kind>",
        f"<title>{content.get('title') or artifact.get('title', '')}</title>",
        f"<subtitle>{content.get('subtitle', '')}</subtitle>",
    ]
    if kind == "deck":
        for i, s in enumerate((content.get("slides", []) or []), 1):
            parts.append(f"\n--- slide {i} ({s.get('type', 'content')}) ---")
            for k in ("title", "subtitle", "heading", "body_md", "image_hint"):
                v = s.get(k)
                if v:
                    parts.append(f"{k}: {v}")
            for b in s.get("bullets", []) or []:
                parts.append(f"  • {b}")
    elif kind == "one_pager":
        hs = content.get("hero_stat") or {}
        parts.append(f"\n[hero] {hs.get('value','')}  ({hs.get('label','')})")
        parts.append(f"\n[lead] {content.get('lead', '')}")
        for s in (content.get("sections", []) or []):
            parts.append(f"\n--- section: {s.get('heading','')} ---")
            parts.append(s.get("body_md", ""))
        if content.get("proof"):
            parts.append("\n[proof]")
            for p in content["proof"]:
                parts.append(f"  • {p}")
        cta = content.get("cta") or {}
        parts.append(f"\n[cta] {cta.get('headline','')} — {cta.get('detail','')}")
    elif kind == "doc":
        parts.append(f"\n[exec summary]\n{content.get('executive_summary','')}")
        for s in (content.get("sections", []) or []):
            parts.append(f"\n--- section: {s.get('heading','')} ---")
            parts.append(s.get("body_md", ""))
        for a in (content.get("appendix", []) or []):
            parts.append(f"\n[appendix: {a.get('heading','')}]\n{a.get('body_md','')}")
        cta = content.get("cta") or {}
        parts.append(f"\n[cta] {cta.get('headline','')} — {cta.get('detail','')}")
    else:
        # Fallback for unknown kinds — dump whatever's there
        parts.append(str(content))
    return "\n".join(parts)


async def critique_artifact(artifact_id: int) -> dict[str, Any]:
    """Critique a previously-drafted artifact. Saves the critique as a
    new artifact (kind="critique") with parent_id pointing at the
    artifact under review.

    The new artifact's content is the structured critique JSON. List
    artifacts filtered by `kind="critique"` to see all reviews; use
    parent_id to find which artifact each critique reviews.
    """
    artifact = await get_artifact(artifact_id)
    if not artifact:
        raise FileNotFoundError(f"artifact #{artifact_id} not found")

    business_slug = artifact["business_slug"]
    audience_slug = artifact.get("audience_slug")
    kit = load_kit(business_slug)

    audience_md = kit.audience(audience_slug) if audience_slug else ""

    user_prompt = (
        f"{kit.render_for_prompt()}\n\n"
        f"<audience slug=\"{audience_slug or 'unspecified'}\">\n"
        f"{audience_md or '(no audience persona on file — judge audience-fit on general principles)'}\n"
        f"</audience>\n\n"
        f"<artifact-under-review>\n"
        f"{_summarize_artifact(artifact)}\n"
        f"</artifact-under-review>\n\n"
        "Critique the artifact now. Return JSON only."
    )

    # No forbidden-phrase scanning on the critique output itself —
    # the critique is meta-text about voice, not voice-compliant copy.
    critique_json = await generate_json(
        system=_CRITIQUE_SYSTEM,
        user=user_prompt,
        forbidden=[],
        text_blob_fn=_critique_text_blob,
        model=CRITIQUE_MODEL,
        max_tokens=4000,
    )

    title = (
        f"Critique of #{artifact_id} — "
        f"{(artifact.get('title') or '')[:60]} "
        f"({critique_json.get('verdict', 'reviewed')}, "
        f"{critique_json.get('overall_score', '?')}/100)"
    )
    saved = await create_artifact(
        business_slug=business_slug,
        kind="critique",
        audience_slug=audience_slug,
        title=title,
        ask=f"critique of artifact #{artifact_id}",
        content=critique_json,
        parent_id=artifact_id,
    )

    # Layer 4 hook: low scores log a self-improvement observation so
    # the queue surfaces patterns over time. Lazy import to avoid
    # circular dependency via the store layer.
    try:
        score = int(critique_json.get("overall_score") or 0)
        if score and score < 60:
            from astra.creators.self_improve import auto_observe_low_critique
            await auto_observe_low_critique(
                critique_artifact_id=int(saved["id"]),
                parent_artifact_id=int(artifact_id),
                business_slug=business_slug,
                overall_score=score,
            )
    except Exception as obs_err:
        logger.warning(
            "[critique] self-improve hook failed: %s", obs_err
        )

    return saved
