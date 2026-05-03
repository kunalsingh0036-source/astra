"""
Analyze a reference website — IA + components + style system + critique.

Given a URL, fetch the page, decompose it into structured analysis that
later draft tools (`draft_site_brief`, `draft_component_spec`) can cite as
"borrow this pattern from <ref-id>".

Limitations (intentional):
- HTML-only fetch via httpx; no headless browser. Pure-SPA sites with
  client-side-rendered content will have limited visibility — Astra
  flags this in the analysis. The trade-off: no Playwright in the
  Railway image (saves ~500MB), no subprocess overhead.
- We extract text + structural cues (semantic HTML5 tags, common
  class-name patterns, inline styles) and feed Claude the cleaned
  content. The model does the higher-level analysis.

Output is saved as kind="site_analysis" so downstream tools can
reference it by artifact id.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from astra.creators._shared import (
    DRAFT_MODEL,
    generate_json,
    join_text_fields,
)
from astra.creators.store import create_artifact, update_artifact_content

logger = logging.getLogger(__name__)


_FETCH_TIMEOUT = 20.0
_FETCH_HEADERS = {
    # Pretend to be a real browser. Some sites block obvious bots.
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
_MAX_HTML_BYTES = 800_000  # cap fetch size — most marketing pages are under 200KB


_HEX_RE = re.compile(r"#[0-9a-fA-F]{6}\b")


def _fetch_html(url: str) -> tuple[str, dict[str, Any]]:
    """Fetch a URL and return (html_text, metadata). Raises on hard failures.

    metadata: status_code, content_type, byte_size, final_url (after redirects).
    """
    with httpx.Client(
        timeout=_FETCH_TIMEOUT,
        follow_redirects=True,
        headers=_FETCH_HEADERS,
    ) as client:
        resp = client.get(url)
    ct = resp.headers.get("content-type", "").lower()
    if "html" not in ct and "xml" not in ct:
        raise ValueError(
            f"URL {url} returned content-type {ct!r}; expected HTML"
        )
    body = resp.text[:_MAX_HTML_BYTES]
    return body, {
        "status_code": resp.status_code,
        "content_type": ct,
        "byte_size": len(resp.content),
        "final_url": str(resp.url),
    }


def _extract_structural_summary(html: str, base_url: str) -> dict[str, Any]:
    """Extract a structural summary the model can analyze without seeing
    the full HTML blob. Saves tokens, focuses the model on what matters.

    Returns:
      title, meta_description, headings (h1..h3), nav_links, sections,
      colors_seen, fonts_seen, scripts (third-party services),
      image_count, form_count, total_text_length.
    """
    soup = BeautifulSoup(html, "html.parser")
    base_host = urlparse(base_url).netloc

    title = (soup.find("title").get_text(strip=True) if soup.find("title") else "")[:160]
    meta_desc = ""
    md = soup.find("meta", attrs={"name": "description"})
    if md and md.get("content"):
        meta_desc = md["content"][:300]

    headings: dict[str, list[str]] = {"h1": [], "h2": [], "h3": []}
    for level in ("h1", "h2", "h3"):
        for h in soup.find_all(level)[:30]:
            txt = h.get_text(" ", strip=True)
            if txt and len(txt) < 200:
                headings[level].append(txt)

    # Navigation: a/links inside <nav>, <header>, or with role="navigation"
    nav_links: list[dict[str, str]] = []
    nav_containers = soup.find_all("nav") + soup.find_all(attrs={"role": "navigation"})
    for nav in nav_containers[:5]:
        for a in nav.find_all("a", href=True)[:15]:
            label = a.get_text(" ", strip=True)
            href = a.get("href", "").strip()
            if label and href and len(label) < 60:
                nav_links.append({"label": label, "href": href})
    if not nav_links:
        # Fallback: any header > a
        for header in soup.find_all("header")[:2]:
            for a in header.find_all("a", href=True)[:15]:
                label = a.get_text(" ", strip=True)
                if label and len(label) < 60:
                    nav_links.append({"label": label, "href": a.get("href", "")})

    # "Sections" inferred from <section>, <main > div>, semantic landmarks
    sections: list[dict[str, str]] = []
    for sec in soup.find_all(["section", "article"])[:25]:
        first_heading = sec.find(["h1", "h2", "h3"])
        text_preview = sec.get_text(" ", strip=True)[:240]
        if not text_preview:
            continue
        sections.append({
            "heading": (first_heading.get_text(strip=True)[:120]
                        if first_heading else ""),
            "text_preview": text_preview,
        })

    # Color hints: search inline styles + <style> blocks + linked CSS hex
    inline_blob = " ".join([
        " ".join(s.get_text() for s in soup.find_all("style")),
        " ".join(t.get("style", "") for t in soup.find_all(style=True)),
    ])
    colors_seen = list({c.lower() for c in _HEX_RE.findall(inline_blob)})[:20]

    # Font hints: look for font-family declarations + Google Fonts links
    font_families: set[str] = set()
    for m in re.finditer(r"font-family\s*:\s*([^;{}]+)", inline_blob, re.I):
        for f in m.group(1).split(","):
            ff = f.strip().strip("'\"")
            if ff and len(ff) < 40 and not ff.startswith("var("):
                font_families.add(ff)
    for link in soup.find_all("link", href=True):
        href = link["href"]
        if "fonts.googleapis.com" in href or "fontshare" in href:
            for m in re.finditer(r"family=([A-Za-z0-9+]+)", href):
                font_families.add(m.group(1).replace("+", " "))

    # Third-party scripts (analytics, frameworks, embeds)
    scripts: set[str] = set()
    for sc in soup.find_all("script", src=True):
        src = sc["src"]
        host = urlparse(src).netloc
        if host and host != base_host and not host.endswith(base_host):
            scripts.add(host)

    # Counts
    image_count = len(soup.find_all("img"))
    form_count = len(soup.find_all("form"))
    total_text = len(soup.get_text(" ", strip=True))

    return {
        "title": title,
        "meta_description": meta_desc,
        "headings": headings,
        "nav_links": nav_links[:20],
        "sections": sections,
        "colors_seen": colors_seen,
        "fonts_seen": sorted(font_families)[:15],
        "third_party_hosts": sorted(scripts)[:15],
        "image_count": image_count,
        "form_count": form_count,
        "total_text_length": total_text,
    }


_ANALYZE_SYSTEM = """You are Astra's creator sub-agent — reference-site analyst.

You receive a structural summary of a webpage (title, headings,
sections, nav, detected colors/fonts, third-party services). Your job
is to decompose it into a useful analysis that downstream draft tools
(`draft_site_brief`, `draft_component_spec`) will cite by id.

Be specific. "Hero with two-column layout, big-number KPI on the
right, animated underline on CTA" beats "modern hero". The whole point
of this analysis is so a future draft can say "borrow the hero treatment
from analysis #N" without re-reading the source site.

Output STRICT JSON matching this schema:

{
  "url": "<final URL after redirects>",
  "page_intent": "<one-sentence — what this page is for>",
  "page_kind": "marketing_home" | "product_page" | "pricing" | "about" | "blog_post" | "documentation" | "saas_app" | "portfolio" | "ecommerce_pdp" | "other",
  "ia_summary": "<one paragraph — what's on this page in what order, with section names>",
  "sections": [
    {
      "position": <integer, 1-N>,
      "type": "hero" | "value_prop" | "features" | "social_proof" | "pricing" | "faq" | "cta_block" | "footer" | "testimonials" | "process" | "team" | "stats" | "logos" | "narrative" | "demo" | "comparison" | "other",
      "summary": "<what this section says/does — concrete, 1-2 sentences>",
      "components_observed": ["<short component-tag>", "..."],
      "copy_quality": "<voice/clarity assessment — short>"
    }
  ],
  "style_system": {
    "tone": "<institutional | editorial | playful | raw | technical | luxury | etc — single word / short phrase>",
    "color_palette": ["<hex codes, 2-6 dominant>"],
    "fonts": ["<font family names — display first, body second>"],
    "density": "minimal" | "standard" | "dense",
    "motion_cues": "<short — what motion is used and where, or 'static'>",
    "imagery_style": "<one sentence — photography vs illustration vs abstract; subject matter; treatment>"
  },
  "functionality_observed": [
    {
      "name": "<feature name — e.g. 'newsletter signup', 'live chat', 'pricing toggle'>",
      "scope": "<what it does>",
      "third_party": "<service if known, e.g. 'Intercom', 'Stripe', or 'unknown'>"
    }
  ],
  "what_works": [
    "<concrete observation — 'Hero CTA is the only element above the fold; reduces decision friction.'>"
  ],
  "what_doesnt": [
    "<concrete observation — 'Pricing section has 6 tiers; should be 3 max for SMB.'>"
  ],
  "borrowable_patterns": [
    {
      "pattern": "<short name — e.g. 'numbered chapter dividers', 'sticky-pill nav with auto-highlight'>",
      "context_for_use": "<when this pattern would suit a different site — be specific about the audience/category fit>"
    }
  ],
  "warnings": [
    "<any caveats — 'Site is JS-rendered; only above-the-fold content was visible to the fetcher.', 'Page redirects to a paywall.', etc.>"
  ]
}

Rules:
1. If the structural summary shows very low total_text_length AND many third-party scripts, flag it in warnings as likely SPA / JS-rendered (HTML-only fetcher missed content).
2. Section types: pick the closest from the enum. "other" only when none fit.
3. borrowable_patterns is the high-value output. Find 3-6 concrete patterns that a smart designer would steal.
4. Be honest in what_works / what_doesnt. The founder will use this to decide what to copy and what to avoid.

Return ONLY the JSON. No prose preamble."""


def _analysis_text_blob(d: dict[str, Any]) -> str:
    parts: list[str] = []
    parts.append(d.get("ia_summary", "") or "")
    parts.extend(d.get("what_works", []) or [])
    parts.extend(d.get("what_doesnt", []) or [])
    for sec in (d.get("sections", []) or []):
        if isinstance(sec, dict):
            parts.append(join_text_fields(sec, ("summary", "copy_quality")))
    return "\n".join(parts)


async def analyze_reference_site(url: str) -> dict[str, Any]:
    """Fetch a URL and produce a structured analysis.

    Returns the saved artifact dict. Stored as kind="site_analysis" so
    downstream tools can reference it by id.

    Checkpointing strategy: this tool runs in two phases. Phase 1
    (fetch + structural summary, ~5-20s) is fast and reliable. Phase 2
    (LLM analysis, can take 30-90s) is where things go wrong — Railway
    deploys, network blips, and SDK timeouts have historically killed
    in-flight calls and lost the work entirely.

    Fix: write a `status='running'` placeholder row immediately after
    phase 1 with the URL + structural summary in `content`. Even if
    phase 2 explodes, the row survives — the user can find it via
    list_creator_artifacts and either resume or read the structural
    data directly. On phase 2 success the row is updated to
    `status='complete'` with the full analysis. On failure we mark
    `status='failed'` with the exception message so future runs can
    distinguish "no attempt" from "tried and broke."
    """
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    try:
        html, fetch_meta = _fetch_html(url)
    except Exception as e:
        raise RuntimeError(f"Failed to fetch {url}: {type(e).__name__}: {e}") from e

    summary = _extract_structural_summary(html, base_url=fetch_meta["final_url"])

    # ── Checkpoint: persist the running row BEFORE the slow LLM call.
    # If the process dies mid-analysis, this row survives with the
    # structural summary so nothing is lost.
    placeholder_title = f"Analysis (running): {summary.get('title','') or url}"[:200]
    placeholder_content: dict[str, Any] = {
        "url": url,
        "final_url": fetch_meta.get("final_url"),
        "structural_summary": summary,
        "fetch_meta": fetch_meta,
        "phase": "awaiting_llm_analysis",
    }
    artifact = await create_artifact(
        business_slug="top-studios",
        kind="site_analysis",
        audience_slug=None,
        title=placeholder_title,
        ask=f"analyze {url}",
        content=placeholder_content,
        status="running",
    )
    artifact_id = int(artifact["id"])
    logger.info("analyze_reference_site checkpoint: id=%s url=%s", artifact_id, url)

    # The user prompt — feed the model the structural summary, not the
    # raw HTML. Saves tokens, focuses the analysis.
    import json as _json
    user_prompt = (
        f"<reference-url>{url}</reference-url>\n"
        f"<final-url>{fetch_meta['final_url']}</final-url>\n"
        f"<status>{fetch_meta['status_code']}</status>\n"
        f"<byte-size>{fetch_meta['byte_size']}</byte-size>\n\n"
        f"<structural-summary>\n{_json.dumps(summary, indent=2)[:12000]}\n</structural-summary>\n\n"
        "Produce the analysis JSON now."
    )

    try:
        analysis = await generate_json(
            system=_ANALYZE_SYSTEM,
            user=user_prompt,
            forbidden=[],  # analyses describe other sites; not voice-bound
            text_blob_fn=_analysis_text_blob,
            model=DRAFT_MODEL,
            max_tokens=4500,
        )
    except Exception as e:
        # Phase 2 broke. Mark the row failed with what we know — the
        # URL and structural summary are still there. Re-raise so the
        # agent surfaces the failure to the user.
        logger.exception("analyze_reference_site phase 2 failed for id=%s", artifact_id)
        await update_artifact_content(
            artifact_id,
            content={
                **placeholder_content,
                "phase": "failed",
                "error": f"{type(e).__name__}: {e}"[:1000],
            },
            status="failed",
            title=f"Analysis (failed): {summary.get('title','') or url}"[:200],
        )
        raise

    # Phase 2 succeeded — flip the row to complete with the full
    # analysis. We keep the structural_summary in content too (under
    # `_raw`) so debugging is possible without re-fetching.
    final_title = f"Analysis: {summary.get('title','') or url}"[:200]
    final_content: dict[str, Any] = {
        **analysis,
        "_raw": {
            "structural_summary": summary,
            "fetch_meta": fetch_meta,
        },
    }
    await update_artifact_content(
        artifact_id,
        content=final_content,
        status="complete",
        title=final_title,
    )
    artifact["title"] = final_title
    artifact["content"] = final_content
    artifact["status"] = "complete"
    return artifact
