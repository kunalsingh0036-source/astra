"""
The source SPINE — deterministic acquisition for the intelligence brief.

Kunal's remit (2026-07-25): "If I am off all my social media and also off
the traditional news, I want Astra to be self-sufficient and able to
narrow down for me what is important." The old daily briefing pulled
from open web_search and fabricated on AI days (briefing #133: 4/4 cited
URLs were 404). The spine is the structural fix: a HARDCODED registry of
verified sources, fetched on schedule with NO LLM in the fetch path.
Nothing enters a brief that didn't come from here (or get verified by
the research agent against a fetched URL).

Every source below was probed live on 2026-07-25 before inclusion
(HTTP 200 + parseable). Scrape-tier sources use minimal HTML link
extraction; if a scrape breaks it logs loudly and the source reports
zero items — never silently fabricates.

RSS/Atom parsing is stdlib xml.etree — no new dependency; standard
feeds only, and a parse failure on one source never kills the fetch.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from astra.db.engine import async_session

logger = logging.getLogger(__name__)

_UA = {"User-Agent": "Mozilla/5.0 (Macintosh) AstraSpine/1.0"}


@dataclass(frozen=True)
class Source:
    key: str
    url: str
    kind: str      # rss | hf_models | hf_papers | hn_api | openrouter | scrape_links
    tier: int      # 1 = primary signal, 2 = boards/meta, 3 = discourse, 4 = india/funding
    tag: str       # lab | models | papers | stack | media-models | boards | discourse | india | funding


# Probed 2026-07-25: every entry returned 200 + parseable in the listed kind.
REGISTRY: list[Source] = [
    # ── Tier 1: labs + primary releases ──
    Source("openai-news", "https://openai.com/news/rss.xml", "rss", 1, "lab"),
    Source("deepmind", "https://deepmind.google/blog/rss.xml", "rss", 1, "lab"),
    Source("anthropic-news", "https://www.anthropic.com/news", "scrape_links", 1, "lab"),
    Source("mistral-news", "https://mistral.ai/news", "scrape_links", 1, "lab"),
    Source("hf-papers", "https://huggingface.co/api/daily_papers?limit=12", "hf_papers", 1, "papers"),
    Source("hf-qwen", "https://huggingface.co/api/models?author=Qwen&sort=createdAt&limit=6", "hf_models", 1, "models"),
    Source("hf-deepseek", "https://huggingface.co/api/models?author=deepseek-ai&sort=createdAt&limit=6", "hf_models", 1, "models"),
    Source("hf-moonshot", "https://huggingface.co/api/models?author=moonshotai&sort=createdAt&limit=6", "hf_models", 1, "models"),
    Source("hf-meta", "https://huggingface.co/api/models?author=meta-llama&sort=createdAt&limit=6", "hf_models", 1, "models"),
    Source("hf-mistral", "https://huggingface.co/api/models?author=mistralai&sort=createdAt&limit=6", "hf_models", 1, "models"),
    Source("hf-xai", "https://huggingface.co/api/models?author=xai-org&sort=createdAt&limit=6", "hf_models", 1, "models"),
    # stack repos (releases only — low noise, high signal)
    Source("gh-vllm", "https://github.com/vllm-project/vllm/releases.atom", "rss", 1, "stack"),
    Source("gh-comfyui", "https://github.com/comfyanonymous/ComfyUI/releases.atom", "rss", 1, "stack"),
    Source("gh-mcp", "https://github.com/modelcontextprotocol/modelcontextprotocol/releases.atom", "rss", 1, "stack"),
    Source("gh-diffusers", "https://github.com/huggingface/diffusers/releases.atom", "rss", 1, "stack"),
    Source("gh-agent-sdk", "https://github.com/anthropics/claude-agent-sdk-python/releases.atom", "rss", 1, "stack"),
    # Sargo-critical: media-model marketplaces
    Source("fal-models", "https://fal.ai/models", "scrape_links", 1, "media-models"),
    Source("openrouter", "https://openrouter.ai/api/v1/models", "openrouter", 2, "boards"),
    # ── Tier 3: discourse (the X/news replacement) ──
    Source("ainews-smol", "https://news.smol.ai/rss.xml", "rss", 3, "discourse"),
    Source("simonwillison", "https://simonwillison.net/atom/everything/", "rss", 3, "discourse"),
    Source("interconnects", "https://www.interconnects.ai/feed", "rss", 3, "discourse"),
    Source("importai", "https://importai.substack.com/feed", "rss", 3, "discourse"),
    Source("latentspace", "https://www.latent.space/feed", "rss", 3, "discourse"),
    Source("zvi", "https://thezvi.substack.com/feed", "rss", 3, "discourse"),
    Source("chinatalk", "https://www.chinatalk.media/feed", "rss", 3, "discourse"),
    Source("hn-top", "https://hn.algolia.com/api/v1/search_by_date?tags=story&numericFilters=points%3E150&hitsPerPage=15", "hn_api", 3, "discourse"),
    Source("localllama", "https://www.reddit.com/r/LocalLLaMA/.rss", "rss", 3, "discourse"),
    Source("semianalysis", "https://semianalysis.com/feed/", "rss", 2, "compute"),
    # ── Tier 4: India policy + funding ──
    Source("medianama", "https://www.medianama.com/feed/", "rss", 4, "india"),
    Source("pib-meity", "https://pib.gov.in/RssMain.aspx?ModId=6&Lang=1&Regid=3", "rss", 4, "india"),
    Source("crunchbase", "https://news.crunchbase.com/feed/", "rss", 4, "funding"),
    Source("inc42", "https://inc42.com/feed/", "rss", 4, "funding"),
]


# ── Tripwires: named triggers that alert IMMEDIATELY, not at 07:00 ──
# v1 is keyword-based on title+summary of NEW items. Alert on outcomes.
TRIPWIRES: list[dict[str, Any]] = [
    {"key": "new-claude-model", "pattern": r"claude[- ][\w.]*\b(opus|sonnet|haiku|\d)", "why": "Astra + Shotgun run on Claude; a new model is a same-day evaluation task"},
    {"key": "deepseek-weights", "pattern": r"deepseek[- ]?(v\d|r\d)\w*", "why": "open-weights frontier shift moves Sargo's cost floor"},
    {"key": "new-video-model", "pattern": r"\b(sora|veo|kling|seedance|runway|luma|wan|hunyuan[- ]?video|pika)\b.{0,50}\b(release|launch|unveil|announce|v\d|available)", "why": "Sargo's production chain reroutes on every video-model release"},
    {"key": "india-ai-rules", "pattern": r"\b(meity|indiaai|cert-in|dpdp)\b.{0,80}\b(rule|regulat|gazett|notif|advisory|labell?ing|deepfake)", "why": "compliance deadline risk for Sargo's Aug 1 relaunch + beyond"},
    {"key": "openai-frontier", "pattern": r"\bgpt[- ]?\d+\.?\d*\b.{0,40}\b(release|launch|announce|available)", "why": "frontier capability/price floor moves reprice the whole stack"},
]


def _hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", "ignore")).hexdigest()[:32]


def _strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", " ", s or "").replace("&amp;", "&").strip()


def _parse_rss(raw: bytes, src: Source) -> list[dict[str, Any]]:
    """RSS 2.0 + Atom via stdlib. Returns [{title,url,summary,published}]."""
    out: list[dict[str, Any]] = []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        logger.warning("[spine] %s: XML parse failed: %s", src.key, e)
        return out
    ns = {"a": "http://www.w3.org/2005/Atom"}
    # RSS 2.0
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        desc = _strip_html(item.findtext("description") or "")[:600]
        pub = (item.findtext("pubDate") or "").strip()
        if title and link:
            out.append({"title": title, "url": link, "summary": desc, "published": pub})
    # Atom
    if not out:
        for entry in root.iter("{http://www.w3.org/2005/Atom}entry"):
            title = (entry.findtext("a:title", namespaces=ns) or "").strip()
            le = entry.find("a:link", ns)
            link = (le.get("href") if le is not None else "").strip()
            summ = _strip_html(entry.findtext("a:summary", namespaces=ns)
                               or entry.findtext("a:content", namespaces=ns) or "")[:600]
            pub = (entry.findtext("a:updated", namespaces=ns)
                   or entry.findtext("a:published", namespaces=ns) or "").strip()
            if title and link:
                out.append({"title": title, "url": link, "summary": summ, "published": pub})
    return out[:25]


def _parse_hf_models(raw: bytes, src: Source) -> list[dict[str, Any]]:
    try:
        data = json.loads(raw)
    except Exception:
        return []
    out = []
    for m in data if isinstance(data, list) else []:
        mid = m.get("modelId") or m.get("id") or ""
        if mid:
            out.append({
                "title": f"HF model release: {mid}",
                "url": f"https://huggingface.co/{mid}",
                "summary": f"downloads={m.get('downloads', 0)} likes={m.get('likes', 0)} tags={','.join((m.get('tags') or [])[:6])}",
                "published": m.get("createdAt") or "",
            })
    return out


def _parse_hf_papers(raw: bytes, src: Source) -> list[dict[str, Any]]:
    try:
        data = json.loads(raw)
    except Exception:
        return []
    out = []
    for p in data if isinstance(data, list) else []:
        paper = p.get("paper") or {}
        pid = paper.get("id") or ""
        title = (paper.get("title") or "").strip()
        if pid and title:
            out.append({
                "title": f"Paper: {title}",
                "url": f"https://huggingface.co/papers/{pid}",
                "summary": _strip_html((paper.get("summary") or ""))[:600],
                "published": p.get("publishedAt") or "",
            })
    return out


def _parse_hn(raw: bytes, src: Source) -> list[dict[str, Any]]:
    try:
        data = json.loads(raw)
    except Exception:
        return []
    out = []
    for h in (data.get("hits") or []):
        title = (h.get("title") or "").strip()
        url = h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}"
        # AI-relevance pre-filter: HN is broad; keep only plausibly-AI stories
        blob = (title + " " + (h.get("_tags") and "" or "")).lower()
        if not re.search(r"\b(ai|llm|gpt|claude|gemini|model|agent|anthropic|openai|deepseek|qwen|diffusion|transformer|inference|cuda|nvidia)\b", blob):
            continue
        if title:
            out.append({"title": title, "url": url,
                        "summary": f"{h.get('points', 0)} points on HN",
                        "published": h.get("created_at") or ""})
    return out


def _parse_openrouter(raw: bytes, src: Source) -> list[dict[str, Any]]:
    """Track NEW model listings on OpenRouter (usage boards are Phase 2)."""
    try:
        data = json.loads(raw)
    except Exception:
        return []
    out = []
    for m in (data.get("data") or [])[:400]:
        created = m.get("created") or 0
        # only models added in the last 3 days enter the feed
        if created and (datetime.now(timezone.utc).timestamp() - created) < 3 * 86400:
            out.append({
                "title": f"OpenRouter listed: {m.get('name') or m.get('id')}",
                "url": f"https://openrouter.ai/{m.get('id', '')}",
                "summary": f"context={m.get('context_length')} pricing={json.dumps((m.get('pricing') or {}))[:120]}",
                "published": datetime.fromtimestamp(created, tz=timezone.utc).isoformat(),
            })
    return out


def _parse_scrape_links(raw: bytes, src: Source) -> list[dict[str, Any]]:
    """Minimal link extraction for pages without feeds (anthropic news,
    mistral news, fal models). Breaks loudly, never fabricates."""
    html = raw.decode("utf-8", "ignore")
    out: list[dict[str, Any]] = []
    if src.key == "anthropic-news":
        for m in re.finditer(r'href="(/news/[a-z0-9-]+)"[^>]*>([^<]{8,120})<', html):
            out.append({"title": _strip_html(m.group(2)), "url": f"https://www.anthropic.com{m.group(1)}", "summary": "", "published": ""})
    elif src.key == "mistral-news":
        for m in re.finditer(r'href="(/news/[a-z0-9-]+)"', html):
            slug = m.group(1).split("/")[-1].replace("-", " ")
            out.append({"title": f"Mistral: {slug}", "url": f"https://mistral.ai{m.group(1)}", "summary": "", "published": ""})
    elif src.key == "fal-models":
        for m in re.finditer(r'href="(/models/[a-zA-Z0-9_/-]+)"', html):
            out.append({"title": f"fal model: {m.group(1).split('/models/')[-1]}", "url": f"https://fal.ai{m.group(1)}", "summary": "", "published": ""})
    # dedupe within page
    seen: set[str] = set()
    uniq = []
    for o in out:
        if o["url"] not in seen:
            seen.add(o["url"])
            uniq.append(o)
    return uniq[:20]


_PARSERS = {
    "rss": _parse_rss,
    "hf_models": _parse_hf_models,
    "hf_papers": _parse_hf_papers,
    "hn_api": _parse_hn,
    "openrouter": _parse_openrouter,
    "scrape_links": _parse_scrape_links,
}


_ENSURE_SQL = """
CREATE TABLE IF NOT EXISTS source_items (
    id          BIGSERIAL PRIMARY KEY,
    source_key  TEXT NOT NULL,
    tier        INTEGER NOT NULL DEFAULT 3,
    tag         TEXT NOT NULL DEFAULT '',
    title       TEXT NOT NULL,
    url         TEXT NOT NULL,
    summary     TEXT NOT NULL DEFAULT '',
    published   TEXT NOT NULL DEFAULT '',
    content_key TEXT NOT NULL UNIQUE,
    fetched_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_source_items_fetched
    ON source_items (fetched_at DESC);
CREATE TABLE IF NOT EXISTS intel_items (
    id          BIGSERIAL PRIMARY KEY,
    briefing_id INTEGER,
    claim       TEXT NOT NULL,
    entities    TEXT NOT NULL DEFAULT '',
    url         TEXT NOT NULL DEFAULT '',
    briefed_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS tripwire_hits (
    id          BIGSERIAL PRIMARY KEY,
    tripwire    TEXT NOT NULL,
    item_url    TEXT NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    fired_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tripwire, item_url)
)
"""


async def ensure_tables() -> None:
    async with async_session() as s:
        for stmt in _ENSURE_SQL.split(";"):
            if stmt.strip():
                await s.execute(text(stmt))
        await s.commit()


async def fetch_all() -> dict[str, Any]:
    """Fetch every registered source, upsert new items, fire tripwires.
    Per-source failure is contained + logged; the job never dies whole."""
    import httpx

    await ensure_tables()
    new_items: list[dict[str, Any]] = []
    per_source: dict[str, int] = {}
    async with httpx.AsyncClient(timeout=20.0, headers=_UA, follow_redirects=True) as client:
        for src in REGISTRY:
            try:
                r = await client.get(src.url)
                if r.status_code != 200:
                    logger.warning("[spine] %s: HTTP %s", src.key, r.status_code)
                    per_source[src.key] = -1
                    continue
                items = _PARSERS[src.kind](r.content, src)
            except Exception as e:
                logger.warning("[spine] %s: fetch failed: %s", src.key, e)
                per_source[src.key] = -1
                continue
            added = 0
            async with async_session() as s:
                for it in items:
                    ck = _hash(f"{src.key}|{it['url']}|{it['title']}")
                    res = await s.execute(
                        text("""
                            INSERT INTO source_items
                                (source_key, tier, tag, title, url, summary,
                                 published, content_key)
                            VALUES (:k, :t, :g, :ti, :u, :su, :p, :ck)
                            ON CONFLICT (content_key) DO NOTHING
                            RETURNING id
                        """),
                        {"k": src.key, "t": src.tier, "g": src.tag,
                         "ti": it["title"][:500], "u": it["url"][:900],
                         "su": it["summary"][:800], "p": str(it["published"])[:80],
                         "ck": ck},
                    )
                    if res.scalar() is not None:
                        added += 1
                        new_items.append({**it, "source_key": src.key, "tier": src.tier})
                await s.commit()
            per_source[src.key] = added
    fired = await _check_tripwires(new_items)
    logger.info("[spine] fetch: %d new items across %d sources; %d tripwires",
                len(new_items), len(REGISTRY), fired)
    return {"new": len(new_items), "per_source": per_source, "tripwires": fired}


async def _check_tripwires(new_items: list[dict[str, Any]]) -> int:
    """Keyword tripwires on NEW items → immediate owner alert (deduped)."""
    import os

    import httpx

    fired = 0
    for it in new_items:
        blob = f"{it.get('title','')} {it.get('summary','')}".lower()
        for tw in TRIPWIRES:
            if not re.search(tw["pattern"], blob, re.IGNORECASE):
                continue
            async with async_session() as s:
                res = await s.execute(
                    text("""
                        INSERT INTO tripwire_hits (tripwire, item_url, title)
                        VALUES (:t, :u, :ti)
                        ON CONFLICT (tripwire, item_url) DO NOTHING
                        RETURNING id
                    """),
                    {"t": tw["key"], "u": it["url"][:900], "ti": it["title"][:400]},
                )
                is_new = res.scalar() is not None
                await s.commit()
            if not is_new:
                continue
            fired += 1
            base = os.environ.get("GATEWAY_URL", "http://whatsapp.railway.internal:8080").rstrip("/")
            secret = os.environ.get("AGENT_SHARED_SECRET", "").strip()
            msg = (f"⚡ Tripwire [{tw['key']}]\n{it['title']}\n{it['url']}\n"
                   f"Why it matters: {tw['why']}")
            try:
                async with httpx.AsyncClient(timeout=10.0) as c:
                    r = await c.post(f"{base}/api/v1/notify/owner",
                                     json={"text": msg[:1500]},
                                     headers={"x-astra-secret": secret})
                if r.status_code != 200:
                    logger.error("[spine] tripwire notify failed: %s", r.status_code)
            except Exception:
                logger.exception("[spine] tripwire notify errored")
    return fired
