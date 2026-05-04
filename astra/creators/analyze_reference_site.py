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

from astra.creators.store import create_artifact

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


# NOTE: The previous nested-LLM-call analysis system has been removed.
# It produced fragile 60-90s tool calls that exceeded the SDK's hook
# callback timeouts and caused turn-wide hangs. The agent driving the
# conversation now produces the analysis natively — see the docstring
# on `analyze_reference_site` below for the full rationale.


async def analyze_reference_site(url: str) -> dict[str, Any]:
    """Fetch a URL and return its structural data — fast, deterministic.

    Returns the saved artifact dict. Stored as kind="site_analysis" so
    downstream tools (draft_site_brief, etc.) can reference it by id.

    ARCHITECTURAL CHANGE (2026-05-04):
    This tool used to make a NESTED LLM CALL — Claude API from inside
    the agent's tool execution path — to produce a structured analysis
    JSON. That was wrong:

      1. It took 60-90s per call. The Claude Agent SDK's bundled CLI
         runs hook callbacks with internal timeouts shorter than that.
         When the tool exceeded the timeout, the CLI threw "Stream
         closed" and the entire turn got poisoned — exactly the
         "1138s no activity" hang the user kept hitting.

      2. It double-charged the API: the outer agent (already a Sonnet
         session) was paused waiting for the tool, while the tool ran
         its own Sonnet call to do analysis the outer agent could do
         natively in its main thread.

      3. It locked the analysis into a fixed JSON schema. The outer
         agent often has more context (kit, audience, intent) than
         this tool does and would produce a better analysis if given
         the raw structural data.

    NEW BEHAVIOR:
    Phase 1 (fetch + structural extraction) only — ~5-15s, no LLM.
    Returns the structural summary directly: title, headings, nav,
    sections, detected colors/fonts, third-party scripts, counts. The
    artifact stores this raw data with status='complete' immediately.

    The OUTER agent (the one driving the conversation) reads the data
    via the tool's text response and produces the analysis itself in
    its main thread — where it already has reasoning capacity, full
    context, and direct visibility into the user's actual question.
    Downstream tools that cite the artifact (draft_site_brief) get
    the same raw data they were already getting from the `_raw` field.
    """
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    try:
        html, fetch_meta = _fetch_html(url)
    except Exception as e:
        raise RuntimeError(f"Failed to fetch {url}: {type(e).__name__}: {e}") from e

    summary = _extract_structural_summary(html, base_url=fetch_meta["final_url"])

    title = f"Site data: {summary.get('title','') or url}"[:200]
    content: dict[str, Any] = {
        "url": url,
        "final_url": fetch_meta.get("final_url"),
        "structural_summary": summary,
        "fetch_meta": fetch_meta,
        # Empty placeholders for downstream-tool back-compat. Old
        # call sites that read these fields get an empty value
        # rather than KeyError, signaling "agent did the analysis
        # in conversation, not as structured fields."
        "page_intent": "",
        "page_kind": "",
        "ia_summary": "",
        "sections": [],
        "style_system": {},
        "borrowable_patterns": [],
        "what_works": [],
        "what_doesnt": [],
        "warnings": [
            "This artifact contains raw structural data only. The "
            "calling agent produced the analysis directly in chat — "
            "see the conversation transcript for sections/patterns/"
            "borrow recommendations."
        ],
    }
    artifact = await create_artifact(
        business_slug="top-studios",
        kind="site_analysis",
        audience_slug=None,
        title=title,
        ask=f"analyze {url}",
        content=content,
        status="complete",
    )
    logger.info(
        "analyze_reference_site: id=%s url=%s sections=%d colors=%d fonts=%d",
        artifact["id"],
        url,
        len(summary.get("sections") or []),
        len(summary.get("colors_seen") or []),
        len(summary.get("fonts_seen") or []),
    )
    return artifact
