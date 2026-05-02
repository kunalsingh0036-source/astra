"""
Render artifacts to binary formats (PDF for now; PPTX in Phase B2).

The strategy: HTML+CSS rendered to PDF via WeasyPrint. Brand styling
lives in the Jinja2 template's CSS, parameterized by CSS variables
that we inject from the kit's brand.yml at render time. Same template
generates a HelmTech deck or a BAY deck — only the variables differ.

Output flow:
  artifact_id → kit + content → HTML string → PDF bytes → R2 upload
  → return signed URL.

R2 details mirror the backup script's pattern; we share the env-var
interface so secret rotation works for both.
"""

from __future__ import annotations

import io
import logging
import os
from pathlib import Path
from typing import Any

import boto3
import jinja2
from botocore.client import Config

from astra.creators.kits import load_kit
from astra.creators.store import get_artifact, update_artifact_render_key

logger = logging.getLogger(__name__)


_TEMPLATES_DIR = Path(__file__).parent / "templates"


def _jinja_env() -> jinja2.Environment:
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=jinja2.select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def _r2_client():
    """S3 client pointed at Cloudflare R2.

    Uses the SAME env vars as the backup service — set on whichever
    service runs the renderer. If the renderer runs in `stream`
    (creator tools live there), stream needs R2_* env vars too.
    """
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


# ── deck → PDF ──────────────────────────────────────────────────────


async def render_deck_pdf(artifact_id: int) -> dict[str, Any]:
    """Render a deck artifact to PDF and upload to R2.

    Returns: dict with `r2_key`, `signed_url`, `byte_size`. Updates
    the artifact row's `r2_pdf_key` so future render calls can detect
    cached output.
    """
    artifact = await get_artifact(artifact_id)
    if not artifact:
        raise FileNotFoundError(f"artifact #{artifact_id} not found")
    if artifact["kind"] != "deck":
        raise ValueError(
            f"artifact #{artifact_id} is kind={artifact['kind']!r}, not 'deck'"
        )

    kit = load_kit(artifact["business_slug"])
    content = artifact["content"] or {}

    # Render HTML
    env = _jinja_env()
    template = env.get_template("deck.html.j2")
    html = template.render(
        title=content.get("title", artifact["title"]),
        subtitle=content.get("subtitle", ""),
        slides=content.get("slides", []),
        colors=kit.colors,
        fonts=kit.fonts,
        company_name=kit.name,
        footer_enabled=(kit.brand.get("output", {}) or {}).get("slide_footer", True),
        slide_numbers=(kit.brand.get("output", {}) or {}).get("slide_numbers", True),
    )

    # HTML → PDF via WeasyPrint
    try:
        from weasyprint import HTML
    except ImportError as e:
        raise RuntimeError(
            "weasyprint not installed; add it to pyproject.toml deps"
        ) from e

    pdf_bytes = HTML(string=html).write_pdf()
    if pdf_bytes is None:
        raise RuntimeError("weasyprint returned no bytes")

    # Upload to R2
    key = f"creators/{artifact['business_slug']}/decks/{artifact_id:06d}.pdf"
    s3 = _r2_client()
    s3.put_object(
        Bucket=_r2_bucket(),
        Key=key,
        Body=pdf_bytes,
        ContentType="application/pdf",
    )
    await update_artifact_render_key(artifact_id, kind="pdf", key=key)

    # Signed URL good for 7 days — reasonable balance for sharing
    # to investors/partners while still being revocable on key rotation.
    signed_url = s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": _r2_bucket(), "Key": key},
        ExpiresIn=7 * 24 * 3600,
    )

    return {
        "artifact_id": artifact_id,
        "r2_key": key,
        "signed_url": signed_url,
        "byte_size": len(pdf_bytes),
    }
