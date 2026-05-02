"""
Render a multi-page site preview as a zip of HTML files, uploaded to R2.

Given a site_brief artifact + the set of page_content artifacts that
draft against it, produce a navigable static HTML preview the founder
can review before any production build.

Each page renders to <slug>.html with cross-page navigation; bundle
goes to R2 as a zip. Founder downloads, unzips, opens index.html.

Note: this is a PREVIEW, not production HTML. Inline CSS, no build
step, no JS framework. Goal is review-quality, not ship-quality.
"""

from __future__ import annotations

import io
import logging
import os
import zipfile
from typing import Any

import boto3
from botocore.client import Config

from astra.creators.kits import load_kit
from astra.creators.render import _jinja_env  # reuse existing
from astra.creators.store import (
    create_artifact,
    get_artifact,
    list_artifacts,
    update_artifact_render_key,
)

logger = logging.getLogger(__name__)


def _r2_client():
    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        config=Config(signature_version="s3v4"),
        region_name="auto",
    )


def _r2_bucket() -> str:
    return os.environ.get("R2_BUCKET", "astra-backups")


def _safe_filename(slug: str) -> str:
    """Coerce a page slug to a filesystem-safe filename stem.

    Drafters sometimes return URL-style slugs ("/", "/platform") when
    we asked for kebab-case. Be lenient at render time:
      "/"          → "home"
      "/platform"  → "platform"
      "platform"   → "platform"
      ""           → "page"
    """
    s = (slug or "").strip().lstrip("/")
    if not s:
        return "home"
    # Allow letters, digits, hyphens; replace anything else with '-'
    import re as _re
    s = _re.sub(r"[^a-z0-9-]+", "-", s.lower())
    s = _re.sub(r"-+", "-", s).strip("-")
    return s or "page"


async def _collect_page_contents(
    site_brief_id: int,
) -> dict[str, dict[str, Any]]:
    """Find all page_content artifacts whose parent is this brief.

    Returns: {filesystem_safe_slug: page_content_dict, ...}
    """
    pages: dict[str, dict[str, Any]] = {}
    rows = await list_artifacts(kind="page_content", limit=200)
    for row in rows:
        full = await get_artifact(row["id"])
        if not full or full.get("parent_id") != site_brief_id:
            continue
        raw_slug = (full.get("content") or {}).get("page_slug") or ""
        safe = _safe_filename(raw_slug)
        if safe not in pages:
            # Mutate page_slug in the in-memory copy so the template
            # uses the safe form for cross-page links.
            content = dict(full.get("content") or {})
            content["page_slug"] = safe
            full = dict(full)
            full["content"] = content
            pages[safe] = full
    return pages


def _render_one_page(
    *,
    page_content: dict[str, Any],
    sitemap: list[dict[str, Any]],
    kit,
) -> str:
    """Render a single page_content artifact's content dict to HTML."""
    env = _jinja_env()
    template = env.get_template("site_preview.html.j2")
    page = page_content.get("content") or {}
    return template.render(
        page=page,
        meta=page.get("meta") or {},
        sitemap=sitemap,
        colors=kit.colors,
        fonts=kit.fonts,
        company_name=kit.name,
        tagline=kit.tagline_short,
    )


async def render_site_preview(site_brief_id: int) -> dict[str, Any]:
    """Render all page_content children of a site_brief into a zip preview.

    Returns dict with `r2_key`, `signed_url`, `byte_size`, `page_count`.
    Saves a new artifact (kind="site_preview") whose r2_pdf_key holds
    the zip's R2 key — we reuse the pdf-key column for "rendered binary"
    rather than adding a third column.
    """
    brief = await get_artifact(site_brief_id)
    if not brief:
        raise FileNotFoundError(f"site_brief #{site_brief_id} not found")
    if brief.get("kind") != "site_brief":
        raise ValueError(
            f"artifact #{site_brief_id} is kind={brief['kind']!r}, not 'site_brief'"
        )

    business_slug = brief["business_slug"]
    kit = load_kit(business_slug)
    brief_content = brief.get("content") or {}
    sitemap = brief_content.get("sitemap", []) or []
    if not sitemap:
        raise ValueError(f"site_brief #{site_brief_id} has empty sitemap")

    # Sanitize sitemap slugs so cross-page links match the rendered filenames.
    sitemap = [
        {**p, "slug": _safe_filename(p.get("slug", ""))}
        for p in sitemap
    ]

    pages = await _collect_page_contents(site_brief_id)
    if not pages:
        raise ValueError(
            f"No page_content artifacts found for site_brief #{site_brief_id}. "
            "Run draft_page_content for each page in the brief first."
        )

    # Save a placeholder artifact first so we have an id for the R2 key.
    preview_artifact = await create_artifact(
        business_slug=business_slug,
        kind="site_preview",
        title=f"Preview: {brief.get('title', '')}",
        ask=f"site preview for brief #{site_brief_id}",
        content={
            "site_brief_id": site_brief_id,
            "page_slugs": sorted(pages.keys()),
            "page_count": len(pages),
        },
        parent_id=site_brief_id,
    )
    preview_id = preview_artifact["id"]

    # Build the zip in memory.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for page_slug, page_artifact in pages.items():
            html = _render_one_page(
                page_content=page_artifact,
                sitemap=sitemap,
                kit=kit,
            )
            zf.writestr(f"{page_slug}.html", html)
        # Convention: home.html → index.html as default entry
        if "home" in pages:
            home_html = _render_one_page(
                page_content=pages["home"],
                sitemap=sitemap,
                kit=kit,
            )
            zf.writestr("index.html", home_html)
        # Plus a tiny readme so the unzipper sees what's in here
        readme = (
            f"{kit.name} — site preview\n"
            f"Brief id: {site_brief_id}\n"
            f"Pages ({len(pages)}): {', '.join(sorted(pages.keys()))}\n"
            f"\nOpen index.html in any browser. Cross-page links work locally.\n"
            f"This is a PREVIEW for review, not production HTML.\n"
        )
        zf.writestr("README.txt", readme)

    zip_bytes = buf.getvalue()

    # Upload
    key = f"creators/{business_slug}/site-previews/{preview_id:06d}.zip"
    s3 = _r2_client()
    s3.put_object(
        Bucket=_r2_bucket(),
        Key=key,
        Body=zip_bytes,
        ContentType="application/zip",
    )
    await update_artifact_render_key(preview_id, kind="pdf", key=key)
    signed_url = s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": _r2_bucket(), "Key": key},
        ExpiresIn=7 * 24 * 3600,
    )

    return {
        "artifact_id": preview_id,
        "site_brief_id": site_brief_id,
        "r2_key": key,
        "signed_url": signed_url,
        "byte_size": len(zip_bytes),
        "page_count": len(pages),
        "page_slugs": sorted(pages.keys()),
    }
