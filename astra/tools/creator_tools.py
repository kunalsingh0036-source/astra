"""
MCP tools for Astra's creator capability.

Phase B (MVP) tools:
  list_business_kits     — what kits are loadable
  read_business_kit      — peek at a kit's contents
  draft_deck             — generate a deck artifact (LLM-driven)
  render_deck_pdf        — render a deck artifact to PDF (uploads to R2)
  list_creator_artifacts — what's been generated

Phase B2 tools:
  draft_one_pager        — single-page sales sheet / fact sheet
  draft_doc              — long-form document (proposal, brief, MoU, white paper)
  draft_brand_kit        — generate a complete brand kit for a CLIENT (Top Studios productization)
  critique_artifact      — Haiku-cheap quality review pass
  generate_hero_image    — brand-aware image prompt (renders if GEMINI_API_KEY set)
  render_one_pager_pdf   — render one-pager to A4 PDF
  render_doc_pdf         — render doc to multi-page PDF
  render_deck_pptx       — render deck to editable .pptx

Phase B3 tools (website maker):
  analyze_reference_site — fetch + decompose a URL into IA + style + critique
  draft_site_brief       — kit + audience + goals + refs → sitemap + IA + style direction
  draft_page_content     — site brief + page slug → all on-page copy + image hints
  draft_component_spec   — component-level handoff for designer / dev
  render_site_preview    — multi-page navigable HTML preview (zip on R2)
"""

from __future__ import annotations

import json

from claude_agent_sdk import tool, create_sdk_mcp_server

from astra.creators.analyze_reference_site import analyze_reference_site
from astra.creators.critique import critique_artifact
from astra.creators.draft import draft_deck
from astra.creators.draft_brand_kit import draft_brand_kit
from astra.creators.draft_caption_set import draft_caption_set
from astra.creators.draft_carousel import draft_carousel
from astra.creators.draft_component_spec import draft_component_spec
from astra.creators.draft_doc import draft_doc
from astra.creators.draft_hashtag_set import draft_hashtag_set
from astra.creators.draft_one_pager import draft_one_pager
from astra.creators.draft_page_content import draft_page_content
from astra.creators.draft_site_brief import draft_site_brief
from astra.creators.draft_subtitle_set import draft_subtitle_set
from astra.creators.draft_thread import draft_thread
from astra.creators.draft_video_brief import draft_video_brief
from astra.creators.draft_voiceover_script import draft_voiceover_script
from astra.creators.image import generate_hero_image
from astra.creators.kits import list_kits, load_kit
from astra.creators.render import (
    render_deck_pdf,
    render_doc_pdf,
    render_one_pager_pdf,
)
from astra.creators.render_pptx import render_deck_pptx
from astra.creators.render_site_preview import render_site_preview
from astra.creators.store import list_artifacts
from astra.tools.code_editor_tools import CODE_EDITOR_TOOLS
from astra.tools.kit_editor_tools import KIT_EDITOR_TOOLS


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


# ── Phase B3: Website maker ─────────────────────────────────────────


@tool(
    "analyze_reference_site",
    "Fetch a website URL and decompose it into structured analysis: page "
    "intent, IA, sections, components observed, style system (colors, "
    "fonts, density, motion), functionality, what works, what doesn't, "
    "and borrowable patterns. Use BEFORE drafting a site brief when you "
    "want to cite reference sites Kunal said to study. Note: HTML-only "
    "fetch — JS-rendered SPA content may have limited visibility (the "
    "tool flags this in warnings).",
    {"url": str},
)
async def analyze_reference_site_tool(args: dict) -> dict:
    url = (args.get("url") or "").strip()
    if not url:
        return {"content": [{"type": "text", "text": "analyze_reference_site: url required"}]}
    try:
        artifact = await analyze_reference_site(url)
    except Exception as e:
        return {"content": [{"type": "text", "text": f"Analysis failed: {type(e).__name__}: {e}"}]}

    c = artifact["content"]
    sections = c.get("sections", []) or []
    patterns = c.get("borrowable_patterns", []) or []
    works = c.get("what_works", []) or []
    doesnt = c.get("what_doesnt", []) or []
    summary = (
        f"Analysis #{artifact['id']} of {c.get('url','')}\n"
        f"  Page kind: {c.get('page_kind','?')}\n"
        f"  Intent:    {c.get('page_intent','')}\n"
        f"  Sections observed ({len(sections)}):\n"
    )
    for s in sections:
        summary += f"    {s.get('position','?'):>2}. [{s.get('type','?'):14}] {(s.get('summary','') or '')[:80]}\n"
    style = c.get("style_system") or {}
    summary += (
        f"  Style: tone={style.get('tone','?')}, density={style.get('density','?')}\n"
        f"  Palette: {style.get('color_palette',[])}\n"
        f"  Fonts:   {style.get('fonts',[])}\n"
        f"  Borrowable patterns ({len(patterns)}):\n"
    )
    for p in patterns:
        summary += f"    • {p.get('pattern','?')}: {p.get('context_for_use','')[:100]}\n"
    if works:
        summary += "  What works:\n"
        for w in works[:3]:
            summary += f"    + {w[:120]}\n"
    if doesnt:
        summary += "  What doesn't:\n"
        for w in doesnt[:3]:
            summary += f"    - {w[:120]}\n"
    if c.get("warnings"):
        summary += "  ⚠ Warnings:\n"
        for w in c["warnings"]:
            summary += f"    {w[:120]}\n"
    return {"content": [{"type": "text", "text": summary}]}


@tool(
    "draft_site_brief",
    "Draft a complete site brief for a portfolio company or client kit. "
    "Output: sitemap (4-9 pages), per-page IA (sections + intent + "
    "components + content brief), style direction extending the brand "
    "kit into web specifics, functionality requirements, performance + "
    "a11y baselines. Optionally cites reference site analyses by id. "
    "Returns an artifact id; pair with draft_page_content for each page.",
    {
        "business": str,
        "audience": str,
        "primary_goal": str,
        "site_kind": str,           # marketing_site (default) | saas_app | portfolio | ecommerce | docs | blog | campaign_microsite
        "reference_ids": str,       # comma-separated list of analyze_reference_site artifact ids
        "context": str,
    },
)
async def draft_site_brief_tool(args: dict) -> dict:
    business = (args.get("business") or "").strip()
    audience = (args.get("audience") or "").strip()
    primary_goal = (args.get("primary_goal") or "").strip()
    site_kind = (args.get("site_kind") or "marketing_site").strip()
    refs_csv = (args.get("reference_ids") or "").strip()
    context = (args.get("context") or "").strip()
    if not (business and audience and primary_goal):
        return {"content": [{"type": "text", "text": (
            "draft_site_brief requires: business, audience, primary_goal."
        )}]}

    ref_ids: list[int] = []
    if refs_csv:
        try:
            ref_ids = [int(x.strip()) for x in refs_csv.split(",") if x.strip()]
        except ValueError:
            return {"content": [{"type": "text", "text": (
                "reference_ids must be a comma-separated list of integer artifact ids"
            )}]}

    try:
        artifact = await draft_site_brief(
            business_slug=business,
            audience_slug=audience,
            primary_goal=primary_goal,
            site_kind=site_kind,
            reference_analysis_ids=ref_ids or None,
            context=context,
        )
    except FileNotFoundError as e:
        return {"content": [{"type": "text", "text": f"Cannot draft: {e}"}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"Draft failed: {type(e).__name__}: {e}"}]}

    c = artifact["content"]
    sitemap = c.get("sitemap", []) or []
    summary = (
        f"Drafted site brief #{artifact['id']}\n"
        f"  Title: {artifact['title']}\n"
        f"  Kind: {c.get('site_kind','?')}\n"
        f"  Primary goal: {c.get('primary_goal','')}\n"
        f"  Sitemap ({len(sitemap)} pages):\n"
    )
    for p in sitemap:
        n = len(p.get("sections", []) or [])
        summary += f"    /{p.get('slug','?'):20} {p.get('title','?'):28} ({n} sections, kind={p.get('kind','?')})\n"
    sd = c.get("style_direction") or {}
    summary += (
        f"  Style direction: tone={sd.get('tone','?')}, density={sd.get('density','?')}\n"
        f"  Functionality items: {len(c.get('functionality',[]))}\n"
    )
    summary += (
        f"\nNext: for each page, run draft_page_content "
        f"(site_brief_id={artifact['id']}, page_slug=<slug>).\n"
        f"Then render_site_preview(site_brief_id={artifact['id']})."
    )
    return {"content": [{"type": "text", "text": summary}]}


@tool(
    "draft_page_content",
    "Draft all on-page copy for ONE page of a site (after the brief is done). "
    "Input: site_brief artifact id + the page slug from its sitemap. "
    "Output: meta tags, every section's heading + subhead + body + bullets "
    "+ items + CTAs + image hints, plus footer copy and global CTAs. "
    "Run for each page in the sitemap before rendering the preview.",
    {
        "site_brief_id": int,
        "page_slug": str,
        "context": str,
    },
)
async def draft_page_content_tool(args: dict) -> dict:
    bid = int(args.get("site_brief_id") or 0)
    slug = (args.get("page_slug") or "").strip()
    context = (args.get("context") or "").strip()
    if not (bid and slug):
        return {"content": [{"type": "text", "text": (
            "draft_page_content requires: site_brief_id, page_slug"
        )}]}
    try:
        artifact = await draft_page_content(
            site_brief_id=bid,
            page_slug=slug,
            context=context,
        )
    except FileNotFoundError as e:
        return {"content": [{"type": "text", "text": f"Cannot draft: {e}"}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"Draft failed: {type(e).__name__}: {e}"}]}

    c = artifact["content"]
    sections = c.get("sections", []) or []
    meta = c.get("meta") or {}
    summary = (
        f"Drafted page #{artifact['id']} ({slug})\n"
        f"  Title: {c.get('title','')}\n"
        f"  SEO title: {meta.get('title','')[:80]}\n"
        f"  Sections ({len(sections)}):\n"
    )
    for s in sections:
        h = s.get("heading", "") or s.get("subheading", "") or "(no heading)"
        summary += f"    [{s.get('type','?'):14}] {h[:70]}\n"
    return {"content": [{"type": "text", "text": summary}]}


@tool(
    "draft_component_spec",
    "Produce an implementation-ready spec for ONE component (hero, feature_card, "
    "pricing_card, testimonial, etc.). Includes layout, slots, interaction, "
    "responsive behavior, accessibility, image direction, recommended libraries, "
    "and edge-case handling. Optionally references a parent site_brief or "
    "page_content artifact for context.",
    {
        "business": str,
        "component_type": str,
        "intent": str,
        "page_context": str,         # e.g. 'home > hero'
        "page_content_id": int,      # optional
        "site_brief_id": int,        # optional
        "audience": str,             # optional persona slug
        "context": str,
    },
)
async def draft_component_spec_tool(args: dict) -> dict:
    business = (args.get("business") or "").strip()
    ctype = (args.get("component_type") or "").strip()
    intent = (args.get("intent") or "").strip()
    page_context = (args.get("page_context") or "").strip()
    page_id = int(args.get("page_content_id") or 0) or None
    brief_id = int(args.get("site_brief_id") or 0) or None
    audience = (args.get("audience") or "").strip() or None
    context = (args.get("context") or "").strip()
    if not (business and ctype and intent):
        return {"content": [{"type": "text", "text": (
            "draft_component_spec requires: business, component_type, intent"
        )}]}
    try:
        artifact = await draft_component_spec(
            business_slug=business,
            component_type=ctype,
            intent=intent,
            page_context=page_context,
            page_content_id=page_id,
            site_brief_id=brief_id,
            audience_slug=audience,
            context=context,
        )
    except FileNotFoundError as e:
        return {"content": [{"type": "text", "text": f"Cannot draft: {e}"}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"Draft failed: {type(e).__name__}: {e}"}]}

    c = artifact["content"]
    structure = c.get("structure") or {}
    slots = structure.get("slots") or []
    summary = (
        f"Component spec #{artifact['id']}\n"
        f"  Type: {c.get('component_type','?')}\n"
        f"  Context: {c.get('context','?')}\n"
        f"  Intent: {c.get('intent','')}\n"
        f"  Slots ({len(slots)}): {[s.get('name','?') for s in slots]}\n"
        f"  Layout: {(structure.get('layout','') or '')[:160]}\n"
        f"  States/edge cases: {len(c.get('states_and_edge_cases',[]))}\n"
        f"  Implementation notes: {len(c.get('implementation_notes',[]))}\n"
    )
    return {"content": [{"type": "text", "text": summary}]}


@tool(
    "render_site_preview",
    "Render all the page_content drafts that belong to a site_brief into a "
    "navigable multi-page HTML preview, bundle as a zip, upload to R2. "
    "Returns a 7-day signed URL. The preview lets the founder click around "
    "the site (sections, CTAs, cross-page links) before any production "
    "build. Inline CSS, brand-kit colors + fonts, image placeholders show "
    "the image_hint text rather than rendered images.",
    {"site_brief_id": int},
)
async def render_site_preview_tool(args: dict) -> dict:
    bid = int(args.get("site_brief_id") or 0)
    if not bid:
        return {"content": [{"type": "text", "text": "render_site_preview: site_brief_id required"}]}
    try:
        result = await render_site_preview(bid)
    except Exception as e:
        return {"content": [{"type": "text", "text": f"Render failed: {type(e).__name__}: {e}"}]}
    return {"content": [{"type": "text", "text": (
        f"Rendered site preview\n"
        f"  Brief id:     {result['site_brief_id']}\n"
        f"  Preview id:   #{result['artifact_id']}\n"
        f"  Pages:        {result['page_count']} ({', '.join(result['page_slugs'])})\n"
        f"  R2 key:       {result['r2_key']}\n"
        f"  Size:         {result['byte_size']:,} bytes\n"
        f"  URL (7-day):\n  {result['signed_url']}"
    )}]}


# ── Phase B4: Social media ──────────────────────────────────────────


@tool(
    "draft_carousel",
    "Draft a social-media carousel — slide-by-slide copy + image direction "
    "+ caption + hashtags + first-comment + best-post-time hint. Tunes to "
    "platform conventions (LinkedIn 7-12 slides, Instagram 6-10, X 4-7). "
    "Returns artifact id; the JSON is structured for direct use by a "
    "designer or a future render_carousel_pdf tool.",
    {
        "business": str,
        "audience": str,
        "topic": str,
        "platform": str,            # linkedin (default) | instagram | twitter
        "slide_count_hint": int,    # optional override
        "context": str,
    },
)
async def draft_carousel_tool(args: dict) -> dict:
    business = (args.get("business") or "").strip()
    audience = (args.get("audience") or "").strip()
    topic = (args.get("topic") or "").strip()
    platform = (args.get("platform") or "linkedin").strip()
    slide_count = int(args.get("slide_count_hint") or 0) or None
    context = (args.get("context") or "").strip()
    if not (business and audience and topic):
        return {"content": [{"type": "text", "text": (
            "draft_carousel requires: business, audience, topic"
        )}]}
    try:
        artifact = await draft_carousel(
            business_slug=business, audience_slug=audience, topic=topic,
            platform=platform, slide_count_hint=slide_count, context=context,
        )
    except FileNotFoundError as e:
        return {"content": [{"type": "text", "text": f"Cannot draft: {e}"}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"Draft failed: {type(e).__name__}: {e}"}]}

    c = artifact["content"]
    slides = c.get("slides", []) or []
    summary = (
        f"Drafted carousel #{artifact['id']} ({c.get('platform','?')})\n"
        f"  Topic: {topic}\n"
        f"  Hook: {c.get('hook_promise','')}\n"
        f"  Slides ({len(slides)}):\n"
    )
    for s in slides:
        summary += f"    {s.get('position','?'):>2}. [{s.get('type','?'):10}] {(s.get('headline','') or '')[:70]}\n"
    summary += (
        f"  Caption: {len(c.get('caption','') or ''):,} chars\n"
        f"  Hashtags: {len(c.get('hashtags',[]) or [])} ({c.get('hashtags',[])[:5]})\n"
    )
    return {"content": [{"type": "text", "text": summary}]}


@tool(
    "draft_thread",
    "Draft a long-form thread for X (Twitter) or LinkedIn. Hook post + "
    "sequence of body posts + closing post. Each post stands alone AND "
    "earns the next swipe. Tunes to platform char limits.",
    {
        "business": str,
        "audience": str,
        "topic": str,
        "platform": str,            # twitter (default) | linkedin
        "thread_kind": str,         # narrative | argument | framework | case_study | lessons | thread_essay
        "context": str,
    },
)
async def draft_thread_tool(args: dict) -> dict:
    business = (args.get("business") or "").strip()
    audience = (args.get("audience") or "").strip()
    topic = (args.get("topic") or "").strip()
    platform = (args.get("platform") or "twitter").strip()
    thread_kind = (args.get("thread_kind") or "narrative").strip()
    context = (args.get("context") or "").strip()
    if not (business and audience and topic):
        return {"content": [{"type": "text", "text": (
            "draft_thread requires: business, audience, topic"
        )}]}
    try:
        artifact = await draft_thread(
            business_slug=business, audience_slug=audience, topic=topic,
            platform=platform, thread_kind=thread_kind, context=context,
        )
    except FileNotFoundError as e:
        return {"content": [{"type": "text", "text": f"Cannot draft: {e}"}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"Draft failed: {type(e).__name__}: {e}"}]}

    c = artifact["content"]
    posts = c.get("posts", []) or []
    summary = (
        f"Drafted thread #{artifact['id']} ({c.get('platform','?')}, "
        f"{c.get('thread_kind','?')})\n"
        f"  Topic: {topic}\n"
        f"  Hook: {(c.get('hook_post','') or '')[:140]}\n"
        f"  Body posts ({len(posts)}):\n"
    )
    for p in posts:
        summary += f"    {p.get('position','?'):>2}. {(p.get('body','') or '')[:80]}\n"
    summary += (
        f"  Closing: {(c.get('closing_post','') or '')[:120]}\n"
        f"  Read time: ~{c.get('estimated_read_time_seconds','?')}s\n"
    )
    return {"content": [{"type": "text", "text": summary}]}


@tool(
    "draft_caption_set",
    "Draft 3-5 distinct caption variants for the same topic — different "
    "hook style / length / register, all voice-compliant. Use for A/B "
    "testing or picking best-of-N. Each variant has 'predicted_strength' "
    "noting when it would beat the others.",
    {
        "business": str,
        "audience": str,
        "topic": str,
        "platform": str,            # linkedin (default) | instagram | twitter | facebook
        "variant_count": int,       # 3-5, clamped
        "context": str,
    },
)
async def draft_caption_set_tool(args: dict) -> dict:
    business = (args.get("business") or "").strip()
    audience = (args.get("audience") or "").strip()
    topic = (args.get("topic") or "").strip()
    platform = (args.get("platform") or "linkedin").strip()
    variant_count = int(args.get("variant_count") or 4)
    context = (args.get("context") or "").strip()
    if not (business and audience and topic):
        return {"content": [{"type": "text", "text": (
            "draft_caption_set requires: business, audience, topic"
        )}]}
    try:
        artifact = await draft_caption_set(
            business_slug=business, audience_slug=audience, topic=topic,
            platform=platform, variant_count=variant_count, context=context,
        )
    except FileNotFoundError as e:
        return {"content": [{"type": "text", "text": f"Cannot draft: {e}"}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"Draft failed: {type(e).__name__}: {e}"}]}

    c = artifact["content"]
    variants = c.get("variants", []) or []
    summary = (
        f"Drafted caption set #{artifact['id']} ({c.get('platform','?')}, "
        f"{len(variants)} variants)\n"
        f"  Topic: {topic}\n\n"
    )
    for v in variants:
        summary += (
            f"  ── {v.get('label','?')} ({v.get('length_target','?')}) ──\n"
            f"    Hook: {v.get('hook_style','')}\n"
            f"    First line: {(v.get('first_line','') or '')[:100]}\n"
            f"    Strength: {(v.get('predicted_strength','') or '')[:120]}\n\n"
        )
    return {"content": [{"type": "text", "text": summary}]}


@tool(
    "draft_hashtag_set",
    "Draft an audience-tuned hashtag set with three layers: brand "
    "(company-tied), topical (this post's subject), reach (broader "
    "discovery). Plus per-platform recommendations for which subset to "
    "use, and an avoid-list with rationale.",
    {
        "business": str,
        "topic": str,
        "primary_platform": str,    # linkedin (default) | instagram | twitter | facebook
        "audience": str,            # optional persona
        "context": str,
    },
)
async def draft_hashtag_set_tool(args: dict) -> dict:
    business = (args.get("business") or "").strip()
    topic = (args.get("topic") or "").strip()
    platform = (args.get("primary_platform") or "linkedin").strip()
    audience = (args.get("audience") or "").strip() or None
    context = (args.get("context") or "").strip()
    if not (business and topic):
        return {"content": [{"type": "text", "text": (
            "draft_hashtag_set requires: business, topic"
        )}]}
    try:
        artifact = await draft_hashtag_set(
            business_slug=business, topic=topic, primary_platform=platform,
            audience_slug=audience, context=context,
        )
    except FileNotFoundError as e:
        return {"content": [{"type": "text", "text": f"Cannot draft: {e}"}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"Draft failed: {type(e).__name__}: {e}"}]}

    c = artifact["content"]
    summary = (
        f"Hashtag set #{artifact['id']}\n"
        f"  Topic: {topic}\n"
        f"  Brand tags ({len(c.get('brand_tags',[]))}): {c.get('brand_tags',[])}\n"
        f"  Topical tags ({len(c.get('topical_tags',[]))}): {c.get('topical_tags',[])}\n"
        f"  Reach tags ({len(c.get('reach_tags',[]))}): {c.get('reach_tags',[])}\n"
        f"\n  Recommendations:\n"
    )
    for plat, rec in (c.get("platform_recommendations") or {}).items():
        if isinstance(rec, dict):
            tags = rec.get("use", []) or []
            summary += f"    {plat:10} {len(tags):>2} tags  ({rec.get('rationale','')})\n"
            summary += f"               {tags}\n"
    avoid = c.get("avoid", []) or []
    if avoid:
        summary += f"  Avoid: {avoid[:3]}\n"
    return {"content": [{"type": "text", "text": summary}]}


# ── Phase B5: AI video brief ────────────────────────────────────────


@tool(
    "draft_video_brief",
    "Draft an AI-video brief — shot list + voiceover line-by-line + on-screen "
    "text + per-shot image-gen prompt + b-roll list + music vibe. The "
    "prompt-first artifact for AI video generation: paste the per-shot "
    "prompts into Sora / Runway / Veo (their UIs get the latest models "
    "first), or hand to a human editor. Brand colors anchored in every "
    "image prompt; kit's imagery anti-patterns in negative prompts. "
    "Tunes to format (vertical_short / horizontal_short / square) and "
    "platform (instagram_reels / youtube_shorts / linkedin / etc).",
    {
        "business": str,
        "audience": str,
        "topic": str,
        "runtime_seconds": int,         # 15-90 typical for shorts; up to 180
        "format": str,                  # vertical_short (default) | horizontal_short | square
        "platform": str,                # instagram_reels | youtube_shorts | linkedin | twitter | tiktok | internal_brief
        "context": str,
    },
)
async def draft_video_brief_tool(args: dict) -> dict:
    business = (args.get("business") or "").strip()
    audience = (args.get("audience") or "").strip()
    topic = (args.get("topic") or "").strip()
    runtime = int(args.get("runtime_seconds") or 30)
    fmt = (args.get("format") or "vertical_short").strip()
    platform = (args.get("platform") or "instagram_reels").strip()
    context = (args.get("context") or "").strip()
    if not (business and audience and topic):
        return {"content": [{"type": "text", "text": (
            "draft_video_brief requires: business, audience, topic"
        )}]}
    try:
        artifact = await draft_video_brief(
            business_slug=business, audience_slug=audience, topic=topic,
            runtime_seconds=runtime, format=fmt, platform=platform,
            context=context,
        )
    except FileNotFoundError as e:
        return {"content": [{"type": "text", "text": f"Cannot draft: {e}"}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"Draft failed: {type(e).__name__}: {e}"}]}

    c = artifact["content"]
    shots = c.get("shots", []) or []
    total_dur = sum(float(s.get("duration_seconds") or 0) for s in shots)
    summary = (
        f"Drafted video brief #{artifact['id']}\n"
        f"  Format: {c.get('format')} on {c.get('platform')}\n"
        f"  Runtime: {c.get('runtime_seconds')}s (shot total: {total_dur:.1f}s)\n"
        f"  Logline: {c.get('logline','')}\n"
        f"  Music vibe: {c.get('music_vibe','')[:120]}\n"
        f"  Shots ({len(shots)}):\n"
    )
    for s in shots:
        vo_preview = (s.get('voiceover_text','') or '')[:60]
        summary += (
            f"    {s.get('position','?'):>2}. {s.get('duration_seconds','?')!s:>4}s  "
            f"[{s.get('shot_type','?'):14}] {vo_preview!r}\n"
        )
    bl = c.get("b_roll_list", []) or []
    summary += f"  B-roll items: {len(bl)}\n"
    return {"content": [{"type": "text", "text": summary}]}


@tool(
    "draft_voiceover_script",
    "Draft a voiceover script suitable for TTS (ElevenLabs, OpenAI TTS) "
    "or human read-aloud. Two modes: convert an existing artifact (deck, "
    "doc, one-pager, video_brief, carousel, thread) into spoken form, OR "
    "generate standalone from a topic + duration. Outputs per-segment "
    "timing, delivery cues, emphasis words, pronunciation notes for "
    "acronyms. Spoken-voice discipline is stricter than written.",
    {
        "business": str,                # required if no source_artifact_id
        "audience": str,                # required if no source_artifact_id
        "duration_seconds": int,
        "source_artifact_id": int,      # optional — convert this artifact
        "topic": str,                   # required if no source_artifact_id
        "voice_persona_hint": str,      # optional — 'founder voice', 'institutional narrator', etc.
        "context": str,
    },
)
async def draft_voiceover_script_tool(args: dict) -> dict:
    business = (args.get("business") or "").strip() or None
    audience = (args.get("audience") or "").strip() or None
    duration = int(args.get("duration_seconds") or 60)
    src_id = int(args.get("source_artifact_id") or 0) or None
    topic = (args.get("topic") or "").strip() or None
    persona = (args.get("voice_persona_hint") or "").strip()
    context = (args.get("context") or "").strip()

    if not src_id and not (business and audience and topic):
        return {"content": [{"type": "text", "text": (
            "draft_voiceover_script: provide source_artifact_id OR "
            "(business + audience + topic)"
        )}]}

    try:
        artifact = await draft_voiceover_script(
            business_slug=business, audience_slug=audience,
            duration_seconds=duration,
            source_artifact_id=src_id, topic=topic,
            voice_persona_hint=persona, context=context,
        )
    except FileNotFoundError as e:
        return {"content": [{"type": "text", "text": f"Cannot draft: {e}"}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"Draft failed: {type(e).__name__}: {e}"}]}

    c = artifact["content"]
    segs = c.get("segments", []) or []
    summary = (
        f"Voiceover script #{artifact['id']}\n"
        f"  Duration target: {c.get('duration_seconds')}s (estimated speaking: {c.get('estimated_speaking_seconds')}s)\n"
        f"  Word count: {c.get('estimated_total_words','?')}\n"
        f"  Voice persona: {(c.get('voice_persona','') or '')[:120]}\n"
        f"  Delivery: {(c.get('delivery_notes','') or '')[:140]}\n"
        f"  Segments ({len(segs)}):\n"
    )
    for s in segs:
        summary += (
            f"    {s.get('position','?'):>2}. {s.get('duration_seconds','?')!s:>4}s  "
            f"{(s.get('spoken_text','') or '')[:80]}\n"
        )
    tts = c.get("tts_recommendations") or {}
    if tts:
        summary += f"  TTS hint: voice={tts.get('best_voice_style','?')}, rate={tts.get('speaking_rate','?')}\n"
    return {"content": [{"type": "text", "text": summary}]}


@tool(
    "draft_subtitle_set",
    "Draft multilingual subtitles in SRT-compatible format. Source can be "
    "a voiceover_script or video_brief artifact (timing inherited) OR raw "
    "text + duration. Validates reading rate (≤17 chars/sec ideal). "
    "Defaults to English + Hindi for India-targeted content. Translations "
    "preserve the kit's voice register, NOT word-for-word.",
    {
        "source_artifact_id": int,           # optional — voiceover_script or video_brief
        "raw_text": str,                     # alternative — raw spoken text
        "raw_duration_seconds": int,         # required with raw_text
        "languages": str,                    # comma-separated ISO codes; default 'en,hi'
        "business": str,                     # optional override
    },
)
async def draft_subtitle_set_tool(args: dict) -> dict:
    src_id = int(args.get("source_artifact_id") or 0) or None
    raw_text = (args.get("raw_text") or "").strip() or None
    raw_dur = int(args.get("raw_duration_seconds") or 0) or None
    langs_csv = (args.get("languages") or "en,hi").strip()
    business = (args.get("business") or "").strip() or None
    languages = [s.strip() for s in langs_csv.split(",") if s.strip()]

    if not src_id and not raw_text:
        return {"content": [{"type": "text", "text": (
            "draft_subtitle_set: provide source_artifact_id OR raw_text + raw_duration_seconds"
        )}]}

    try:
        artifact = await draft_subtitle_set(
            source_artifact_id=src_id,
            raw_text=raw_text,
            raw_duration_seconds=raw_dur,
            languages=languages,
            business_slug=business,
        )
    except (FileNotFoundError, ValueError) as e:
        return {"content": [{"type": "text", "text": f"Cannot draft: {e}"}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"Draft failed: {type(e).__name__}: {e}"}]}

    c = artifact["content"]
    langs = c.get("languages", []) or []
    summary = (
        f"Subtitle set #{artifact['id']}\n"
        f"  Source kind: {c.get('source_kind','?')}\n"
        f"  Languages ({len(langs)}):\n"
    )
    for lng in langs:
        lines = lng.get("lines", []) or []
        summary += (
            f"    {lng.get('code','?')} ({lng.get('label','?')})  "
            f"{len(lines)} lines\n"
        )
    val = c.get("validation") or {}
    summary += (
        f"  Total duration: {val.get('total_duration_seconds','?')}s\n"
        f"  Max cps: {val.get('max_cps_seen','?')}\n"
    )
    if val.get("warnings"):
        summary += f"  Warnings: {len(val.get('warnings',[]))}\n"
    return {"content": [{"type": "text", "text": summary}]}


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
        version="0.7.0",
        tools=[
            # Code self-editing (Layer 2 self-modification)
            *CODE_EDITOR_TOOLS,
            # Kit self-editing (Layer 1 self-modification)
            *KIT_EDITOR_TOOLS,
            list_business_kits_tool,
            read_business_kit_tool,
            # Drafters — content artifacts
            draft_deck_tool,
            draft_one_pager_tool,
            draft_doc_tool,
            draft_brand_kit_tool,
            # Drafters — website (Phase B3)
            analyze_reference_site_tool,
            draft_site_brief_tool,
            draft_page_content_tool,
            draft_component_spec_tool,
            # Drafters — social (Phase B4)
            draft_carousel_tool,
            draft_thread_tool,
            draft_caption_set_tool,
            draft_hashtag_set_tool,
            # Drafters — video (Phase B5)
            draft_video_brief_tool,
            draft_voiceover_script_tool,
            draft_subtitle_set_tool,
            # Quality + image
            critique_artifact_tool,
            generate_hero_image_tool,
            # Renderers
            render_deck_pdf_tool,
            render_deck_pptx_tool,
            render_one_pager_pdf_tool,
            render_doc_pdf_tool,
            render_site_preview_tool,
            # Listing
            list_creator_artifacts_tool,
        ],
    )
