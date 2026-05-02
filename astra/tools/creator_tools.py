"""
MCP tools for Astra's creator capability.

Phase B (MVP) tools:
  list_business_kits     — what kits are loadable
  read_business_kit      — peek at a kit's contents
  draft_deck             — generate a deck artifact (LLM-driven)
  render_deck_pdf        — render a deck artifact to PDF (uploads to R2)
  list_creator_artifacts — what's been generated

Phase B2 tools (this commit):
  draft_one_pager        — single-page sales sheet / fact sheet
  draft_doc              — long-form document (proposal, brief, MoU, white paper)
  draft_brand_kit        — generate a complete brand kit for a CLIENT (Top Studios productization)
  critique_artifact      — Haiku-cheap quality review pass
  generate_hero_image    — brand-aware image prompt (renders if GEMINI_API_KEY set)
  render_one_pager_pdf   — render one-pager to A4 PDF
  render_doc_pdf         — render doc to multi-page PDF
  render_deck_pptx       — render deck to editable .pptx
"""

from __future__ import annotations

import json

from claude_agent_sdk import tool, create_sdk_mcp_server

from astra.creators.critique import critique_artifact
from astra.creators.draft import draft_deck
from astra.creators.draft_brand_kit import draft_brand_kit
from astra.creators.draft_doc import draft_doc
from astra.creators.draft_one_pager import draft_one_pager
from astra.creators.image import generate_hero_image
from astra.creators.kits import list_kits, load_kit
from astra.creators.render import (
    render_deck_pdf,
    render_doc_pdf,
    render_one_pager_pdf,
)
from astra.creators.render_pptx import render_deck_pptx
from astra.creators.store import list_artifacts


# ── Discovery ───────────────────────────────────────────────────────


@tool(
    "list_business_kits",
    "List available business kits — Kunal's portfolio companies and "
    "any client kits drafted via Top Studios. Each kit contains the "
    "brand, voice, thesis, audiences, and proof-points needed to "
    "generate brand-consistent artifacts. Use to discover what "
    "businesses you can create artifacts for.",
    {},
)
async def list_business_kits_tool(args: dict) -> dict:
    kits = list_kits()
    if not kits:
        return {"content": [{"type": "text", "text": "No business kits found."}]}
    lines = [f"{len(kits)} business kit{'s' if len(kits) != 1 else ''}:"]
    for k in kits:
        aud = ", ".join(k.get("audiences", []) or []) or "(no audiences yet)"
        lines.append(
            f"  • {k['slug']:14}  {k['name']}\n"
            f"      tagline: {k['tagline_short']}\n"
            f"      audiences: {aud}"
        )
    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


@tool(
    "read_business_kit",
    "Read a single business kit's full contents (brand, voice, thesis, "
    "audiences, proof-points). Use before drafting an artifact when you "
    "need to verify what facts/voice rules the kit provides, or when "
    "Kunal asks 'what does the X kit say about Y'.",
    {"slug": str},
)
async def read_business_kit_tool(args: dict) -> dict:
    slug = (args.get("slug") or "").strip()
    if not slug:
        return {"content": [{"type": "text", "text": "read_business_kit: slug required"}]}
    try:
        kit = load_kit(slug)
    except FileNotFoundError as e:
        return {"content": [{"type": "text", "text": str(e)}]}

    parts = [
        f"# {kit.name} ({kit.slug})",
        f"Tagline: {kit.tagline_short}",
        f"Audiences: {', '.join(sorted(kit.audiences.keys())) or '(none)'}",
        "",
        "## Brand colors",
        "\n".join(f"  {k}: {v}" for k, v in (kit.colors or {}).items()) or "  (TBD)",
        "",
        "## Forbidden phrases",
        "\n".join(f"  - {p}" for p in (kit.brand.get('forbidden_phrases') or [])) or "  (none)",
        "",
        "## Thesis (head)",
        kit.thesis[:1500] + ("…" if len(kit.thesis) > 1500 else ""),
        "",
        "## Voice (head)",
        kit.voice[:1500] + ("…" if len(kit.voice) > 1500 else ""),
        "",
        "## Proof points (head)",
        kit.proof_points[:1500] + ("…" if len(kit.proof_points) > 1500 else ""),
    ]
    return {"content": [{"type": "text", "text": "\n".join(parts)}]}


# ── Draft tools ─────────────────────────────────────────────────────


@tool(
    "draft_deck",
    "Draft a slide deck for a Kunal-portfolio company. Produces a "
    "voice-compliant 8–14 slide deck targeting a specific audience "
    "with a specific ask. Returns an artifact id; render to PDF via "
    "render_deck_pdf or to PowerPoint via render_deck_pptx. Use when "
    "Kunal asks to create a pitch deck, partner deck, sponsor deck, "
    "or any other branded slide deck.",
    {
        "business": str,
        "audience": str,
        "ask": str,
        "context": str,
    },
)
async def draft_deck_tool(args: dict) -> dict:
    business = (args.get("business") or "").strip()
    audience = (args.get("audience") or "").strip()
    ask = (args.get("ask") or "").strip()
    context = (args.get("context") or "").strip()
    if not (business and audience and ask):
        return {"content": [{"type": "text", "text": "draft_deck requires: business, audience, ask."}]}
    try:
        artifact = await draft_deck(
            business_slug=business, audience_slug=audience,
            ask=ask, context=context,
        )
    except FileNotFoundError as e:
        return {"content": [{"type": "text", "text": f"Cannot draft: {e}"}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"Draft failed: {type(e).__name__}: {e}"}]}

    slides = artifact["content"].get("slides", []) or []
    summary = (
        f"Drafted deck #{artifact['id']}\n"
        f"  Title: {artifact['title']}\n"
        f"  Audience: {audience}\n"
        f"  Ask: {ask}\n"
        f"  Slides: {len(slides)}\n\n"
        "Slide outline:\n"
    )
    for i, s in enumerate(slides, 1):
        t = s.get("type", "content")
        title = s.get("title") or s.get("heading") or "(untitled)"
        summary += f"  {i:2}. [{t:7}] {title[:80]}\n"
    summary += (
        f"\nRender to PDF: render_deck_pdf(artifact_id={artifact['id']})\n"
        f"Render to PPTX: render_deck_pptx(artifact_id={artifact['id']})"
    )
    return {"content": [{"type": "text", "text": summary}]}


@tool(
    "draft_one_pager",
    "Draft a single-page sales sheet / fact sheet — denser than a deck "
    "slide, shorter than a doc. Always fits one A4 page. Use for sponsor "
    "leave-behinds, product spec sheets, executive summaries that travel "
    "via email. Returns an artifact id; render with render_one_pager_pdf.",
    {
        "business": str,
        "audience": str,
        "ask": str,
        "context": str,
    },
)
async def draft_one_pager_tool(args: dict) -> dict:
    business = (args.get("business") or "").strip()
    audience = (args.get("audience") or "").strip()
    ask = (args.get("ask") or "").strip()
    context = (args.get("context") or "").strip()
    if not (business and audience and ask):
        return {"content": [{"type": "text", "text": "draft_one_pager requires: business, audience, ask."}]}
    try:
        artifact = await draft_one_pager(
            business_slug=business, audience_slug=audience,
            ask=ask, context=context,
        )
    except FileNotFoundError as e:
        return {"content": [{"type": "text", "text": f"Cannot draft: {e}"}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"Draft failed: {type(e).__name__}: {e}"}]}

    c = artifact["content"]
    sections = c.get("sections", []) or []
    hero = c.get("hero_stat") or {}
    summary = (
        f"Drafted one-pager #{artifact['id']}\n"
        f"  Title: {artifact['title']}\n"
        f"  Audience: {audience}\n"
        f"  Hero stat: {hero.get('value','')} — {hero.get('label','')}\n"
        f"  Sections ({len(sections)}):\n"
    )
    for s in sections:
        summary += f"    - {s.get('heading','(untitled)')}\n"
    cta = c.get("cta") or {}
    summary += f"  CTA: {cta.get('headline','(none)')}\n"
    summary += f"\nRender to PDF: render_one_pager_pdf(artifact_id={artifact['id']})"
    return {"content": [{"type": "text", "text": summary}]}


@tool(
    "draft_doc",
    "Draft a long-form document — proposal, brief, MoU draft, white paper, "
    "or RFP response. Multi-page, institutional register, executive summary "
    "+ sections + optional appendix + explicit CTA. Use for investor data-room "
    "contents, partnership MoUs, formal procurement responses. Returns an "
    "artifact id; render with render_doc_pdf.",
    {
        "business": str,
        "audience": str,
        "ask": str,
        "doc_type": str,   # proposal | brief | mou_draft | white_paper | rfp_response
        "context": str,
    },
)
async def draft_doc_tool(args: dict) -> dict:
    business = (args.get("business") or "").strip()
    audience = (args.get("audience") or "").strip()
    ask = (args.get("ask") or "").strip()
    doc_type = (args.get("doc_type") or "proposal").strip()
    context = (args.get("context") or "").strip()
    if not (business and audience and ask):
        return {"content": [{"type": "text", "text": "draft_doc requires: business, audience, ask."}]}
    try:
        artifact = await draft_doc(
            business_slug=business, audience_slug=audience,
            ask=ask, doc_type=doc_type, context=context,
        )
    except FileNotFoundError as e:
        return {"content": [{"type": "text", "text": f"Cannot draft: {e}"}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"Draft failed: {type(e).__name__}: {e}"}]}

    c = artifact["content"]
    sections = c.get("sections", []) or []
    appendix = c.get("appendix", []) or []
    summary = (
        f"Drafted doc #{artifact['id']} ({c.get('doc_type','doc')})\n"
        f"  Title: {artifact['title']}\n"
        f"  Audience: {audience}\n"
        f"  Sections ({len(sections)}):\n"
    )
    for s in sections:
        summary += f"    - {s.get('heading','(untitled)')}\n"
    if appendix:
        summary += f"  Appendix ({len(appendix)}):\n"
        for a in appendix:
            summary += f"    - {a.get('heading','(untitled)')}\n"
    cta = c.get("cta") or {}
    summary += f"  CTA: {cta.get('headline','(none)')}\n"
    summary += f"\nRender to PDF: render_doc_pdf(artifact_id={artifact['id']})"
    return {"content": [{"type": "text", "text": summary}]}


@tool(
    "draft_brand_kit",
    "Draft a complete brand kit for a CLIENT — Top Studios productization. "
    "Generates brand.yml + voice.md + thesis.md + a primary audience persona "
    "+ proof-points. Writes the kit to disk under business-kits/<slug>/ "
    "(unless write_to_disk=false), so downstream draft tools can immediately "
    "consume the new kit. Use when Kunal acquires a Top Studios brand-kit "
    "client and wants the kit scaffolded fast from research notes.",
    {
        "client_name": str,
        "audience_hint": str,
        "research_input": str,
        "write_to_disk": bool,   # default True
        "force": bool,           # default False — set True to overwrite existing
    },
)
async def draft_brand_kit_tool(args: dict) -> dict:
    client_name = (args.get("client_name") or "").strip()
    audience_hint = (args.get("audience_hint") or "").strip()
    research_input = (args.get("research_input") or "").strip()
    write_to_disk = bool(args.get("write_to_disk", True))
    force = bool(args.get("force", False))
    if not (client_name and audience_hint and research_input):
        return {"content": [{"type": "text", "text": (
            "draft_brand_kit requires: client_name, audience_hint, research_input"
        )}]}
    try:
        artifact = await draft_brand_kit(
            client_name=client_name,
            audience_hint=audience_hint,
            research_input=research_input,
            write_to_disk=write_to_disk,
            force=force,
        )
    except Exception as e:
        return {"content": [{"type": "text", "text": f"Draft failed: {type(e).__name__}: {e}"}]}

    c = artifact["content"]
    summary = (
        f"Drafted brand kit #{artifact['id']}\n"
        f"  Client: {c.get('name','?')}\n"
        f"  Slug: {c.get('slug','?')}\n"
        f"  Tagline: {c.get('tagline_short','?')}\n"
        f"  Voice md: {len(c.get('voice_md','') or ''):,} chars\n"
        f"  Thesis md: {len(c.get('thesis_md','') or ''):,} chars\n"
        f"  Forbidden phrases: {len(c.get('forbidden_phrases', []) or [])}\n"
    )
    primary_aud = c.get("primary_audience") or {}
    if primary_aud:
        summary += f"  Primary audience: {primary_aud.get('slug','?')}\n"
    if "kit_path" in artifact:
        summary += f"  Kit written to: {artifact['kit_path']}\n"
    if "disk_write_error" in artifact:
        summary += f"  Disk write error: {artifact['disk_write_error']}\n"
    return {"content": [{"type": "text", "text": summary}]}


# ── Quality / image tools ───────────────────────────────────────────


@tool(
    "critique_artifact",
    "Run a structured quality critique on a previously-drafted artifact "
    "(deck, doc, or one-pager). Uses Haiku for speed/cost. Returns scores "
    "(voice / audience-fit / factual-grounding / structure) plus concrete "
    "issues with suggested fixes and a top-3 punch list. Use as a pre-flight "
    "check before sending an artifact, or to compare variants.",
    {"artifact_id": int},
)
async def critique_artifact_tool(args: dict) -> dict:
    aid = int(args.get("artifact_id") or 0)
    if not aid:
        return {"content": [{"type": "text", "text": "critique_artifact: artifact_id required"}]}
    try:
        review = await critique_artifact(aid)
    except FileNotFoundError as e:
        return {"content": [{"type": "text", "text": str(e)}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"Critique failed: {type(e).__name__}: {e}"}]}

    c = review["content"]
    scores = (
        f"  voice_compliance:    {c.get('voice_compliance',{}).get('score','?'):>3}\n"
        f"  audience_fit:        {c.get('audience_fit',{}).get('score','?'):>3}\n"
        f"  factual_grounding:   {c.get('factual_grounding',{}).get('score','?'):>3}\n"
        f"  structure_and_flow:  {c.get('structure_and_flow',{}).get('score','?'):>3}\n"
    )
    text = (
        f"Critique #{review['id']} of artifact #{aid}\n"
        f"  Overall: {c.get('overall_score','?')}/100  ({c.get('verdict','?')})\n"
        f"  {c.get('summary','(no summary)')}\n\n"
        f"Scores:\n{scores}\n"
        f"Top fixes:\n"
    )
    for f in (c.get("top_three_fixes", []) or []):
        text += f"  • {f}\n"
    return {"content": [{"type": "text", "text": text}]}


@tool(
    "generate_hero_image",
    "Generate a brand-aware image-generation prompt for a slide hero "
    "or section image. Returns the prompt + negative-prompt + aspect ratio "
    "ready for any image gen model. If GEMINI_API_KEY is set AND the "
    "google-genai package is installed, also renders the actual image and "
    "stores its base64 bytes on the artifact. Use when an artifact needs "
    "imagery and you want it brand-consistent.",
    {
        "business": str,
        "image_hint": str,           # the hint from the artifact (e.g. slide.image_hint)
        "aspect_ratio": str,         # "16:9" | "1:1" | "4:5" | "3:2" | "9:16"
        "artifact_context": str,     # optional surrounding artifact text for context
        "parent_artifact_id": int,   # optional — link to the artifact this image is for
    },
)
async def generate_hero_image_tool(args: dict) -> dict:
    business = (args.get("business") or "").strip()
    image_hint = (args.get("image_hint") or "").strip()
    aspect = (args.get("aspect_ratio") or "16:9").strip()
    ctx = (args.get("artifact_context") or "").strip()
    parent = int(args.get("parent_artifact_id") or 0) or None
    if not (business and image_hint):
        return {"content": [{"type": "text", "text": (
            "generate_hero_image requires: business, image_hint"
        )}]}
    try:
        artifact = await generate_hero_image(
            business_slug=business,
            image_hint=image_hint,
            aspect_ratio=aspect,
            artifact_context=ctx,
            parent_artifact_id=parent,
        )
    except FileNotFoundError as e:
        return {"content": [{"type": "text", "text": str(e)}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"Image-prompt failed: {type(e).__name__}: {e}"}]}

    c = artifact["content"]
    rendered = "✓ rendered (PNG bytes stored)" if c.get("image_b64") else "prompt-only"
    text = (
        f"Image prompt #{artifact['id']} ({rendered})\n"
        f"  aspect: {c.get('aspect_ratio','?')}\n"
        f"  prompt: {c.get('prompt','(none)')}\n"
        f"  negative: {c.get('negative_prompt','(none)')}\n"
        f"  style: {c.get('style_notes','')}\n"
    )
    if not c.get("image_b64") and "image_render_status" in c:
        text += f"  status: {c['image_render_status']}\n"
    return {"content": [{"type": "text", "text": text}]}


# ── Render tools ────────────────────────────────────────────────────


def _render_summary(kind: str, result: dict) -> str:
    return (
        f"Rendered {kind} #{result['artifact_id']}\n"
        f"  R2 key: {result['r2_key']}\n"
        f"  Size:   {result['byte_size']:,} bytes\n"
        f"  URL (7-day):\n  {result['signed_url']}"
    )


@tool(
    "render_deck_pdf",
    "Render a previously-drafted deck artifact to PDF and upload to R2. "
    "Returns a 7-day signed download URL. Uses the company's brand colors "
    "and fonts from the kit.",
    {"artifact_id": int},
)
async def render_deck_pdf_tool(args: dict) -> dict:
    aid = int(args.get("artifact_id") or 0)
    if not aid:
        return {"content": [{"type": "text", "text": "render_deck_pdf: artifact_id required"}]}
    try:
        result = await render_deck_pdf(aid)
    except Exception as e:
        return {"content": [{"type": "text", "text": f"Render failed: {type(e).__name__}: {e}"}]}
    return {"content": [{"type": "text", "text": _render_summary("deck (PDF)", result)}]}


@tool(
    "render_deck_pptx",
    "Render a previously-drafted deck artifact to editable PowerPoint "
    "(.pptx) and upload to R2. Returns a 7-day signed URL. Use when the "
    "recipient needs to edit, annotate, or rearrange the deck — e.g. "
    "investors who want to add notes, partners pasting in their logo.",
    {"artifact_id": int},
)
async def render_deck_pptx_tool(args: dict) -> dict:
    aid = int(args.get("artifact_id") or 0)
    if not aid:
        return {"content": [{"type": "text", "text": "render_deck_pptx: artifact_id required"}]}
    try:
        result = await render_deck_pptx(aid)
    except Exception as e:
        return {"content": [{"type": "text", "text": f"Render failed: {type(e).__name__}: {e}"}]}
    return {"content": [{"type": "text", "text": _render_summary("deck (PPTX)", result)}]}


@tool(
    "render_one_pager_pdf",
    "Render a previously-drafted one-pager artifact to a single-page A4 "
    "PDF and upload to R2. Returns a 7-day signed URL.",
    {"artifact_id": int},
)
async def render_one_pager_pdf_tool(args: dict) -> dict:
    aid = int(args.get("artifact_id") or 0)
    if not aid:
        return {"content": [{"type": "text", "text": "render_one_pager_pdf: artifact_id required"}]}
    try:
        result = await render_one_pager_pdf(aid)
    except Exception as e:
        return {"content": [{"type": "text", "text": f"Render failed: {type(e).__name__}: {e}"}]}
    return {"content": [{"type": "text", "text": _render_summary("one-pager (PDF)", result)}]}


@tool(
    "render_doc_pdf",
    "Render a previously-drafted long-form doc artifact to multi-page "
    "PDF and upload to R2. Returns a 7-day signed URL.",
    {"artifact_id": int},
)
async def render_doc_pdf_tool(args: dict) -> dict:
    aid = int(args.get("artifact_id") or 0)
    if not aid:
        return {"content": [{"type": "text", "text": "render_doc_pdf: artifact_id required"}]}
    try:
        result = await render_doc_pdf(aid)
    except Exception as e:
        return {"content": [{"type": "text", "text": f"Render failed: {type(e).__name__}: {e}"}]}
    return {"content": [{"type": "text", "text": _render_summary("doc (PDF)", result)}]}


# ── Listing ─────────────────────────────────────────────────────────


@tool(
    "list_creator_artifacts",
    "List artifacts the creator has produced (decks, docs, one-pagers, "
    "brand kits, critiques, image prompts), newest first. Optionally "
    "filter by business or kind. Use to find artifact ids for re-rendering "
    "or reviewing.",
    {"business": str, "kind": str, "limit": int},
)
async def list_creator_artifacts_tool(args: dict) -> dict:
    business = (args.get("business") or "").strip() or None
    kind = (args.get("kind") or "").strip() or None
    limit = int(args.get("limit") or 25)
    rows = await list_artifacts(business_slug=business, kind=kind, limit=limit)
    if not rows:
        return {"content": [{"type": "text", "text": "No creator artifacts yet."}]}
    lines = [f"{len(rows)} artifact{'s' if len(rows) != 1 else ''}:"]
    for r in rows:
        pdf = "✓pdf" if r.get("r2_pdf_key") else "    "
        pptx = "✓pptx" if r.get("r2_pptx_key") else "     "
        lines.append(
            f"  #{r['id']:<5} [{r['kind']:11}] {r['business_slug']:14} {pdf} {pptx}  {r['title'][:55]}"
        )
    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


def create_creators_mcp_server():
    return create_sdk_mcp_server(
        name="astra-creators",
        version="0.2.0",
        tools=[
            list_business_kits_tool,
            read_business_kit_tool,
            # Drafters
            draft_deck_tool,
            draft_one_pager_tool,
            draft_doc_tool,
            draft_brand_kit_tool,
            # Quality + image
            critique_artifact_tool,
            generate_hero_image_tool,
            # Renderers
            render_deck_pdf_tool,
            render_deck_pptx_tool,
            render_one_pager_pdf_tool,
            render_doc_pdf_tool,
            # Listing
            list_creator_artifacts_tool,
        ],
    )
