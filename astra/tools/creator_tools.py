"""
MCP tools for Astra's creator capability.

Phase B MVP — tools shipped:
  list_business_kits   — what kits are loadable
  read_business_kit    — peek at a kit's contents
  draft_deck           — generate a deck artifact (LLM-driven)
  render_deck_pdf      — render a deck artifact to PDF (uploads to R2)
  list_creator_artifacts — what's been generated

Phase B2 will add: draft_doc, draft_one_pager, draft_brand_kit,
critique_artifact, render_pptx, generate_hero_image.
"""

from __future__ import annotations

from claude_agent_sdk import tool, create_sdk_mcp_server

from astra.creators.draft import draft_deck
from astra.creators.kits import list_kits, load_kit
from astra.creators.render import render_deck_pdf
from astra.creators.store import list_artifacts


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
        return {
            "content": [{"type": "text", "text": "No business kits found."}]
        }
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
        return {
            "content": [{"type": "text", "text": "read_business_kit: slug required"}]
        }
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


@tool(
    "draft_deck",
    "Draft a slide deck for a Kunal-portfolio company. Produces a "
    "voice-compliant 8–14 slide deck targeting a specific audience "
    "with a specific ask. Returns an artifact id; render to PDF via "
    "render_deck_pdf to get a shareable file. Use when Kunal asks to "
    "create a pitch deck, partner deck, sponsor deck, or any other "
    "branded slide deck.",
    {
        "business": str,    # slug — helmtech, apex, bay, top-studios, or client kit
        "audience": str,    # persona slug from the kit's audiences/
        "ask": str,         # explicit call-to-action that lands on the closing slide
        "context": str,     # optional free-text additional framing/context
    },
)
async def draft_deck_tool(args: dict) -> dict:
    business = (args.get("business") or "").strip()
    audience = (args.get("audience") or "").strip()
    ask = (args.get("ask") or "").strip()
    context = (args.get("context") or "").strip()

    if not (business and audience and ask):
        return {
            "content": [
                {
                    "type": "text",
                    "text": "draft_deck requires: business (kit slug), audience (persona slug), ask (call-to-action).",
                }
            ]
        }

    try:
        artifact = await draft_deck(
            business_slug=business,
            audience_slug=audience,
            ask=ask,
            context=context,
        )
    except FileNotFoundError as e:
        return {"content": [{"type": "text", "text": f"Cannot draft: {e}"}]}
    except Exception as e:
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Draft failed: {type(e).__name__}: {e}",
                }
            ]
        }

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
        f"\nRender to PDF with: render_deck_pdf(artifact_id={artifact['id']})"
    )
    return {"content": [{"type": "text", "text": summary}]}


@tool(
    "render_deck_pdf",
    "Render a previously-drafted deck artifact to PDF and upload to "
    "Cloudflare R2. Returns a signed download URL valid for 7 days. "
    "Use after draft_deck (or to re-render an existing deck if its "
    "content was updated). The PDF uses the company's brand colors + "
    "fonts directly from the kit.",
    {"artifact_id": int},
)
async def render_deck_pdf_tool(args: dict) -> dict:
    aid = int(args.get("artifact_id") or 0)
    if not aid:
        return {
            "content": [{"type": "text", "text": "render_deck_pdf: artifact_id required"}]
        }
    try:
        result = await render_deck_pdf(aid)
    except Exception as e:
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Render failed: {type(e).__name__}: {e}",
                }
            ]
        }
    return {
        "content": [
            {
                "type": "text",
                "text": (
                    f"Rendered deck #{aid} → PDF\n"
                    f"  R2 key: {result['r2_key']}\n"
                    f"  Size:   {result['byte_size']:,} bytes\n"
                    f"  URL (7-day):\n  {result['signed_url']}"
                ),
            }
        ]
    }


@tool(
    "list_creator_artifacts",
    "List artifacts the creator has produced (decks, docs, etc.), "
    "newest first. Optionally filter by business or kind. Use when "
    "Kunal asks 'what decks have I drafted' or to find an artifact id "
    "for re-rendering.",
    {"business": str, "kind": str, "limit": int},
)
async def list_creator_artifacts_tool(args: dict) -> dict:
    business = (args.get("business") or "").strip() or None
    kind = (args.get("kind") or "").strip() or None
    limit = int(args.get("limit") or 25)
    rows = await list_artifacts(business_slug=business, kind=kind, limit=limit)
    if not rows:
        return {
            "content": [{"type": "text", "text": "No creator artifacts yet."}]
        }
    lines = [f"{len(rows)} artifact{'s' if len(rows) != 1 else ''}:"]
    for r in rows:
        rendered = "✓ pdf" if r.get("r2_pdf_key") else "  pdf"
        lines.append(
            f"  #{r['id']:<5} [{r['kind']:9}] {r['business_slug']:14} {rendered}  {r['title'][:60]}"
        )
    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


def create_creators_mcp_server():
    return create_sdk_mcp_server(
        name="astra-creators",
        version="0.1.0",
        tools=[
            list_business_kits_tool,
            read_business_kit_tool,
            draft_deck_tool,
            render_deck_pdf_tool,
            list_creator_artifacts_tool,
        ],
    )
