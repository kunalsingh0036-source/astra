"""
Draft a complete brand kit for a CLIENT (Top Studios productized service).

Architecture: per-file calls rather than one mega-JSON.

Why per-file: the original "one JSON with voice_md + thesis_md + audience_md
+ proof_points_md as long string fields" approach fails ~5-10% of the time
because long markdown blobs inside JSON strings hit edge cases (unescaped
control chars, embedded quotes, model running over token budget mid-string).

Per-file calls:
  1. structure   — brand.yml fields (JSON, no long markdown) + forbidden_phrases
  2. voice       — pure markdown text (voice.md)
  3. thesis      — pure markdown text (thesis.md)
  4. audience    — pure markdown text (audiences/<slug>.md)
  5. proof       — pure markdown text (content/proof-points.md)

Each call has a small, focused output. JSON parsing risk drops to ~0%
(only the structure call returns JSON, and it's small). The trade-off
is 5 LLM calls instead of 1 — slightly more expensive (~5x output tokens
total) but reliability matters more for a productized service.

The output is saved both as a creator_artifacts row (for tracking) AND
written to disk under business-kits/<slug>/ so the kit becomes
immediately loadable by load_kit().
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import yaml

from astra.creators._shared import (
    DRAFT_MODEL,
    generate_json,
    get_anthropic_key,
    join_text_fields,
    strip_code_fences,
)
from astra.creators.kits import _kits_root  # noqa: PLC2701 — internal helper, intentional reuse
from astra.creators.store import create_artifact

logger = logging.getLogger(__name__)


# ── Per-file generation prompts ─────────────────────────────────────


_STRUCTURE_SYSTEM = """You are Astra's creator sub-agent — brand-kit structure generator.

Given a client name, audience hint, and research input, produce the
STRUCTURED brand-kit metadata as JSON. This is the first of five
focused calls; each later call produces one of the markdown files
(voice, thesis, audience, proof points) separately.

Output STRICT JSON matching this schema (NO long markdown blobs here):

{
  "slug": "<client-slug — lowercase-kebab-case, 3-30 chars, filesystem-safe>",
  "name": "<client formal name>",
  "tagline_short": "<5-9 words>",
  "tagline_long": "<15-30 words — the full positioning sentence>",
  "about": "<2-3 sentences — core mission/positioning>",
  "brand": {
    "colors": {
      "primary":   "<hex — dominant brand color>",
      "secondary": "<hex — accent>",
      "surface":   "<hex — background/surface>",
      "ink":       "<hex — body text>",
      "muted":     "<hex — secondary text>"
    },
    "typography": {
      "display": {"family": "<font name>", "fallback": "<comma-list>"},
      "body":    {"family": "<font name>", "fallback": "<comma-list>"}
    },
    "imagery": "<one paragraph — visual style. specific not generic. e.g. 'high-contrast geometric with terminal motifs' not 'modern and clean'>"
  },
  "forbidden_phrases": [
    "<phrase>", "<phrase>"
  ],
  "primary_audience_slug": "<filesystem-safe slug for the primary audience persona file>"
}

Rules:
1. Slug: filesystem-safe (lowercase letters, digits, hyphens; 3-30 chars).
2. Forbidden phrases: include category-generic banned words ("world-class",
   "best-in-class", "cutting-edge", "revolutionary", "disruptive") PLUS any
   phrases the client's voice reveals as banned (e.g. if they refer to users
   as "members" not "customers", flag the wrong term).
3. Colors: pick from research if the client has an established palette.
   Otherwise pick a defensible default that matches the imagery description.
4. Imagery: specific. "Architectural high-contrast" beats "modern and clean".
5. primary_audience_slug should be derivable from audience_hint (e.g.
   "fintech-compliance-lead" → "fintech-compliance-lead").

Return ONLY the JSON. No prose preamble. No markdown fences."""


_VOICE_SYSTEM = """You are Astra's creator sub-agent — voice.md generator.

Given a client's structured brand metadata + research input, produce
a complete voice.md file as MARKDOWN (not JSON).

Sections required:
  # Voice — <client name>
  ## Tone in three words
  ## How sentences look (length, hedging, person, active/passive, lists)
  ## What <client> sounds like — voice samples
     - QUOTE VERBATIM from the research input where the client's own
       writing is provided. Verbatim samples are canonical reference
       for the drafter.
  ## Words and phrases <client> DOES use
  ## Words and phrases <client> NEVER uses
     - includes the forbidden_phrases from structure
     - plus any voice-specific bans the research reveals
  ## Signatures and sign-offs
  ## Voice the audience should hear

Rules:
1. If research has the client's own writing, QUOTE VERBATIM in voice samples.
2. Be specific. "The voice sounds direct, no hedging, declarative" beats
   "professional and friendly".
3. Output PURE MARKDOWN. No JSON wrapping. No code fences around the whole
   document. Markdown code fences are fine inside the doc for code samples.

Return only the markdown body. No preamble."""


_THESIS_SYSTEM = """You are Astra's creator sub-agent — thesis.md generator.

Given a client's structured brand metadata + research input, produce
a complete thesis.md file as MARKDOWN.

Sections required:
  # <client> thesis
  ## In one sentence
  ## What <client> does (or sells)
  ## The wedge — what makes this defensible
  ## Why now
  ## Competitive positioning (vs the 2-3 obvious alternatives)
  ## What <client> is NOT
  ## The team (where research provides credentials)
  ## Forcing functions / milestones (if known)

Rules:
1. NEVER invent traction, customer names, or financial numbers. Where
   research is silent, write "TBD — client to provide".
2. Thesis must be specific to THIS client. Generic mission statements
   fail. Find the actual structural argument.
3. Output PURE MARKDOWN.

Return only the markdown body. No preamble."""


_AUDIENCE_SYSTEM = """You are Astra's creator sub-agent — audience persona generator.

Given a client's structured brand metadata, audience hint, and research
input, produce a complete audience-persona markdown file for the
primary audience.

Sections required:
  # Audience — <Persona name from audience_hint>
  ## Who they are (2-3 sentences, specific)
  ## What they care about (top 3)
  ## What they discount
  ## Likely questions / objections (3 questions)
  ## How to open (concrete instruction for slide 1 / first paragraph)
  ## How to close (concrete instruction for the ask)
  ## Common objections + how to handle (3-5 objections, each with a one-line response)
  ## What's the WIN here? (concrete outcome — meeting, PO, signed contract, etc.)

Rules:
1. Specific. "A procurement manager at a Fortune India 500" beats "buyer".
2. The "What they care about" list is what they VOTE WITH for, not what
   they say they care about.
3. Output PURE MARKDOWN.

Return only the markdown body. No preamble."""


_PROOF_SYSTEM = """You are Astra's creator sub-agent — proof-points.md generator.

Given a client's research input, produce a complete content/proof-points.md
file as MARKDOWN. This is the canonical fact ledger; the drafter cites
ONLY from here.

Sections required:
  # Proof points — <client>
  ## Customers / clients (named only with permission per research)
  ## Traction metrics (with "as of <YYYY-MM>" dates)
  ## Team
  ## Press / coverage
  ## Awards / recognition
  ## Testimonials (only if research provides quotes WITH attribution)
  ## Capabilities / scope
  ## Competitive positioning
  ## Forcing functions / milestones
  ## Open / sensitive items (do not cite without client's go)

Rules:
1. NEVER invent customer names, traction numbers, awards, or testimonials.
   Research is the only source.
2. Where research is silent, write "TBD — <description>" so the client
   can fill it in.
3. Date every traction number ("as of YYYY-MM").
4. Output PURE MARKDOWN.

Return only the markdown body. No preamble."""


# ── Generation helpers ─────────────────────────────────────────────


async def _gen_markdown(*, system: str, user: str, max_tokens: int = 4000) -> str:
    """Single-call markdown generation. No JSON parsing — return the model's
    raw text (with code fences stripped if added)."""
    import anthropic

    key = get_anthropic_key()
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set; cannot draft brand kit")

    client = anthropic.AsyncAnthropic(api_key=key)
    resp = await client.messages.create(
        model=DRAFT_MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    text_out = "\n".join(b.text for b in resp.content if hasattr(b, "text")).strip()
    return strip_code_fences(text_out)


def _structure_text_blob(d: dict[str, Any]) -> str:
    return join_text_fields(
        d, ("name", "tagline_short", "tagline_long", "about")
    )


# ── Disk write ─────────────────────────────────────────────────────


_SLUG_RE = re.compile(r"[^a-z0-9-]+")


def _safe_slug(slug: str) -> str:
    """Force a slug to filesystem-safe form."""
    s = (slug or "").strip().lower()
    s = re.sub(r"\s+", "-", s)
    s = _SLUG_RE.sub("", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s[:30] or "client"


def _write_kit_to_disk(
    *,
    slug: str,
    structure: dict[str, Any],
    voice_md: str,
    thesis_md: str,
    audience_slug: str,
    audience_md: str,
    proof_points_md: str,
    force: bool = False,
) -> Path:
    """Write all five files to business-kits/<slug>/.

    Raises FileExistsError if the directory exists and force=False.
    """
    root = _kits_root()
    if not slug:
        raise ValueError("brand_kit missing valid slug")

    kit_dir = root / slug
    if kit_dir.exists() and not force:
        raise FileExistsError(
            f"business-kit '{slug}' already exists at {kit_dir}. "
            "Pass force=True to overwrite."
        )
    kit_dir.mkdir(parents=True, exist_ok=True)
    (kit_dir / "audiences").mkdir(exist_ok=True)
    (kit_dir / "content").mkdir(exist_ok=True)
    (kit_dir / "content" / "logos").mkdir(exist_ok=True)

    brand_yml_data = {
        "name": structure.get("name", slug.title()),
        "slug": slug,
        "tagline_short": structure.get("tagline_short", ""),
        "tagline_long": structure.get("tagline_long", ""),
        "about": structure.get("about", ""),
        "brand": structure.get("brand", {}) or {},
        "voice_file": "voice.md",
        "thesis_file": "thesis.md",
        "audiences_dir": "audiences/",
        "content_dir": "content/",
        "forbidden_phrases": structure.get("forbidden_phrases", []) or [],
        "output": {
            "doc_page_size": "A4",
            "slide_aspect": "16:9",
            "slide_footer": True,
            "slide_numbers": True,
        },
    }
    (kit_dir / "brand.yml").write_text(
        "# Auto-generated by Astra (Top Studios brand-kit tool).\n"
        "# Review and refine before producing client artifacts.\n\n"
        + yaml.safe_dump(brand_yml_data, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    (kit_dir / "voice.md").write_text(voice_md or "", encoding="utf-8")
    (kit_dir / "thesis.md").write_text(thesis_md or "", encoding="utf-8")
    (kit_dir / "content" / "proof-points.md").write_text(
        proof_points_md or "", encoding="utf-8"
    )

    aud_slug = _safe_slug(audience_slug)
    (kit_dir / "audiences" / f"{aud_slug}.md").write_text(
        audience_md or "", encoding="utf-8"
    )

    return kit_dir


# ── The orchestration ─────────────────────────────────────────────


async def draft_brand_kit(
    *,
    client_name: str,
    audience_hint: str,
    research_input: str,
    write_to_disk: bool = True,
    force: bool = False,
) -> dict[str, Any]:
    """Generate a brand kit for a client across 5 focused LLM calls.

    Args:
      client_name: the client's formal name
      audience_hint: a short slug-style hint about the primary audience
        (e.g. "institutional-buyer", "tier-1-investor", "athlete-recruit")
      research_input: free-text — website copy, founder interviews, materials.
        The richer the input, the better the kit.
      write_to_disk: if True, write the generated files to business-kits/<slug>/
      force: if True and write_to_disk, overwrite an existing kit

    Returns: the saved artifact dict with the structured + markdown content,
    plus `kit_path` if write_to_disk succeeded.
    """
    # Common context bundle every call sees
    base_user = (
        f"<client-name>{client_name}</client-name>\n"
        f"<audience-hint>{audience_hint}</audience-hint>\n\n"
        f"<research-input>\n{research_input[:30000]}\n</research-input>\n\n"
    )

    # Forbidden-phrase pre-check for the structure call (the markdown
    # calls don't go through generate_json — they return raw markdown).
    base_forbidden = [
        "world-class", "best-in-class", "cutting-edge",
        "revolutionary", "disruptive", "leverage synergies",
    ]

    # 1. structure (JSON) — defines the slug + brand fields + forbidden list
    logger.info("[brand-kit] generating structure for %s", client_name)
    structure = await generate_json(
        system=_STRUCTURE_SYSTEM,
        user=base_user + "Build the structured brand metadata now. Return JSON only.",
        forbidden=base_forbidden,
        text_blob_fn=_structure_text_blob,
        max_tokens=2500,
    )
    slug = _safe_slug(structure.get("slug", ""))
    if not slug:
        raise ValueError("structure call did not return a usable slug")
    structure["slug"] = slug
    audience_slug = _safe_slug(structure.get("primary_audience_slug", "") or audience_hint)

    # 2-5. markdown files — each gets the structure as additional context
    structure_context = (
        f"<structured-metadata>\n"
        f"name: {structure.get('name')}\n"
        f"slug: {slug}\n"
        f"tagline_short: {structure.get('tagline_short')}\n"
        f"about: {structure.get('about')}\n"
        f"forbidden_phrases: {structure.get('forbidden_phrases')}\n"
        f"</structured-metadata>\n\n"
    )

    logger.info("[brand-kit] generating voice.md")
    voice_md = await _gen_markdown(
        system=_VOICE_SYSTEM,
        user=base_user + structure_context + "Produce the voice.md content now.",
        max_tokens=4000,
    )

    logger.info("[brand-kit] generating thesis.md")
    thesis_md = await _gen_markdown(
        system=_THESIS_SYSTEM,
        user=base_user + structure_context + "Produce the thesis.md content now.",
        max_tokens=4000,
    )

    logger.info("[brand-kit] generating audience persona %s", audience_slug)
    audience_md = await _gen_markdown(
        system=_AUDIENCE_SYSTEM,
        user=base_user + structure_context + (
            f"<audience-slug>{audience_slug}</audience-slug>\n\n"
            "Produce the audience-persona markdown now."
        ),
        max_tokens=3000,
    )

    logger.info("[brand-kit] generating proof-points.md")
    proof_points_md = await _gen_markdown(
        system=_PROOF_SYSTEM,
        user=base_user + structure_context + "Produce the proof-points.md content now.",
        max_tokens=3000,
    )

    # Bundle for storage
    bundle = {
        "slug": slug,
        "name": structure.get("name"),
        "tagline_short": structure.get("tagline_short"),
        "tagline_long": structure.get("tagline_long"),
        "about": structure.get("about"),
        "brand": structure.get("brand"),
        "forbidden_phrases": structure.get("forbidden_phrases", []),
        "primary_audience": {
            "slug": audience_slug,
            "audience_md": audience_md,
        },
        "voice_md": voice_md,
        "thesis_md": thesis_md,
        "proof_points_md": proof_points_md,
    }

    title = f"Brand kit — {bundle.get('name') or client_name}"
    artifact = await create_artifact(
        business_slug="top-studios",
        kind="brand_kit",
        audience_slug=audience_hint or None,
        title=title,
        ask=f"Generate brand kit for client: {client_name}",
        content=bundle,
    )

    if write_to_disk:
        try:
            kit_path = _write_kit_to_disk(
                slug=slug,
                structure=structure,
                voice_md=voice_md,
                thesis_md=thesis_md,
                audience_slug=audience_slug,
                audience_md=audience_md,
                proof_points_md=proof_points_md,
                force=force,
            )
            artifact["kit_path"] = str(kit_path)
        except FileExistsError as e:
            artifact["disk_write_error"] = str(e)
        except Exception as e:
            logger.exception("[brand-kit] disk write failed")
            artifact["disk_write_error"] = f"{type(e).__name__}: {e}"

    return artifact
