"""
Browser MCP — lightweight fetch + search tools.

This isn't a full headless browser. It complements the SDK's built-in
WebFetch/WebSearch tools with:
  - browser_fetch: raw HTTP GET with the URL returned as clean text,
    title, status, and final URL after redirects. Useful when you
    need to see the structure of a page (not just a summary), or
    hit a URL with known cookies.
  - browser_search: DuckDuckGo HTML endpoint (no API key, no rate
    limit). Returns up to N clean title+url+snippet results, useful
    as a second opinion to WebSearch.

A future iteration can swap the HTTP client for Playwright + chromium
for JS-heavy pages; the tool surface stays the same.
"""

from __future__ import annotations

import html
import re
import ssl
import urllib.parse
import urllib.request

from claude_agent_sdk import tool, create_sdk_mcp_server

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15"


def _ssl_context() -> ssl.SSLContext:
    """Build an SSL context that uses certifi's bundled CAs. macOS
    Python 3.14 installers don't always populate the system trust
    store, which trips urllib on HTTPS. Falls back to the default
    context if certifi isn't importable."""
    try:
        import certifi  # type: ignore[import-not-found]
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


_SSL_CTX = _ssl_context()


def _fetch(url: str, timeout: int = 10) -> tuple[str, int, str]:
    """Return (body, status, final_url). Raises on network error."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,*/*"})
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
        body = resp.read().decode("utf-8", errors="replace")
        return body, resp.status, resp.geturl()


_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_WS_RE = re.compile(r"[ \t]+")
_MULTI_NL_RE = re.compile(r"\n{3,}")


def _html_to_text(body: str) -> tuple[str, str]:
    """Strip HTML → plain text. Returns (title, text)."""
    title_match = re.search(r"<title[^>]*>(.*?)</title>", body, re.DOTALL | re.IGNORECASE)
    title = html.unescape((title_match.group(1) or "").strip()) if title_match else ""

    cleaned = _SCRIPT_RE.sub("", body)
    # Preserve paragraph / block boundaries before stripping tags
    cleaned = re.sub(r"</(p|div|li|h[1-6]|br|tr)[^>]*>", "\n", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"<br[^>]*>", "\n", cleaned, flags=re.IGNORECASE)
    cleaned = _TAG_RE.sub("", cleaned)
    cleaned = html.unescape(cleaned)
    cleaned = _WS_RE.sub(" ", cleaned)
    cleaned = _MULTI_NL_RE.sub("\n\n", cleaned).strip()

    return title, cleaned


@tool(
    "browser_fetch",
    "Fetch a URL and return its text content (HTML stripped). Returns "
    "title, final URL after redirects, HTTP status, and plain text body "
    "truncated to 10k chars. Complements WebFetch for when you need the "
    "raw-ish structure of a page rather than a summary.",
    {"url": str, "max_chars": int},
)
async def browser_fetch_tool(args: dict) -> dict:
    url = str(args.get("url") or "").strip()
    if not url:
        return {"content": [{"type": "text", "text": "browser_fetch: url required"}]}
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    max_chars = int(args.get("max_chars") or 10000)

    try:
        body, status, final_url = _fetch(url)
    except Exception as e:
        return {
            "content": [
                {"type": "text", "text": f"browser_fetch error: {e.__class__.__name__}: {e}"}
            ]
        }

    title, text = _html_to_text(body)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n…[truncated]"

    parts = [
        f"title: {title or '(no title)'}",
        f"url:   {final_url}",
        f"status: {status}",
        "",
        text,
    ]
    return {"content": [{"type": "text", "text": "\n".join(parts)}]}


@tool(
    "browser_search",
    "Search the web via DuckDuckGo (HTML endpoint, no API key). Returns "
    "up to `limit` results with title, URL, and snippet. Use when "
    "WebSearch is rate-limited or unavailable.",
    {"query": str, "limit": int},
)
async def browser_search_tool(args: dict) -> dict:
    q = str(args.get("query") or "").strip()
    if not q:
        return {"content": [{"type": "text", "text": "browser_search: query required"}]}
    limit = max(1, min(15, int(args.get("limit") or 8)))
    url = "https://duckduckgo.com/html/?q=" + urllib.parse.quote(q)

    try:
        body, _status, _final_url = _fetch(url)
    except Exception as e:
        return {
            "content": [
                {"type": "text", "text": f"browser_search error: {e.__class__.__name__}: {e}"}
            ]
        }

    # DDG HTML result pattern — title link, snippet.
    result_re = re.compile(
        r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>.*?'
        r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
        re.DOTALL,
    )
    hits = result_re.findall(body)[:limit]

    if not hits:
        return {"content": [{"type": "text", "text": f"No results for: {q}"}]}

    lines = [f"{len(hits)} results for {q!r}:", ""]
    for i, (raw_url, raw_title, raw_snip) in enumerate(hits, 1):
        title = html.unescape(_TAG_RE.sub("", raw_title)).strip()
        snippet = html.unescape(_TAG_RE.sub("", raw_snip)).strip()
        # DDG wraps hrefs as /l/?kh=...&uddg=<target>; unwrap when possible.
        target = raw_url
        if "uddg=" in raw_url:
            parsed = urllib.parse.urlparse(raw_url)
            qs = urllib.parse.parse_qs(parsed.query)
            target = qs.get("uddg", [raw_url])[0]
        lines.append(f"{i}. {title}")
        lines.append(f"   {target}")
        lines.append(f"   {snippet[:300]}")
        lines.append("")

    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


def create_browser_mcp_server():
    return create_sdk_mcp_server(
        name="astra-browser",
        version="0.1.0",
        tools=[browser_fetch_tool, browser_search_tool],
    )
