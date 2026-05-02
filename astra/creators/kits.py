"""
Business-kit loader.

A "kit" is a directory under business-kits/<slug>/ with a known
shape (see business-kits/README.md). load_kit() reads it from disk
into a BusinessKit dataclass that the creator tools pass to the LLM
as a single prompt-context bundle.

Kits are read on every call rather than cached at boot — they're
small (sub-MB total), reading is fast (filesystem reads), and live
reads mean Kunal can edit a voice.md file and the next draft
reflects the change without a service restart.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


def _kits_root() -> Path:
    """Resolve the business-kits directory.

    Order: BUSINESS_KITS_DIR env var (set by Dockerfile to /app/business-kits)
    → astra/../business-kits relative to this module → cwd/business-kits.
    The env var path wins so the Railway container always uses the
    image-baked location even when cwd shifts.
    """
    env = os.environ.get("BUSINESS_KITS_DIR", "").strip()
    if env:
        p = Path(env)
        if p.exists():
            return p
    # Source-tree fallback (developer machine running outside Docker)
    p = Path(__file__).resolve().parents[2] / "business-kits"
    if p.exists():
        return p
    # Last resort
    return Path.cwd() / "business-kits"


@dataclass
class BusinessKit:
    """One company's brand + voice + content bundle.

    All fields are loaded from disk. Markdown sections are kept as
    raw strings — the LLM consumes them verbatim. brand.yml is
    parsed because the renderer needs the structured fields.
    """

    slug: str
    brand: dict[str, Any]               # parsed brand.yml
    voice: str = ""                     # voice.md contents
    thesis: str = ""                    # thesis.md contents
    audiences: dict[str, str] = field(default_factory=dict)   # slug → md
    proof_points: str = ""              # content/proof-points.md
    kit_path: Path | None = None        # absolute path on disk

    @property
    def name(self) -> str:
        return self.brand.get("name", self.slug.title())

    @property
    def tagline_short(self) -> str:
        return self.brand.get("tagline_short", "")

    @property
    def colors(self) -> dict[str, str]:
        return (self.brand.get("brand", {}) or {}).get("colors", {}) or {}

    @property
    def fonts(self) -> dict[str, dict[str, str]]:
        return (self.brand.get("brand", {}) or {}).get("typography", {}) or {}

    def audience(self, slug: str) -> str:
        """Return the persona file's markdown body, or empty string."""
        return self.audiences.get(slug, "")

    def render_for_prompt(self) -> str:
        """Compact view for the LLM prompt — sections labeled, char-budgeted."""
        bits: list[str] = [
            f"<business-kit name=\"{self.name}\" slug=\"{self.slug}\">",
            f"<tagline>{self.tagline_short}</tagline>",
            f"<about>{self.brand.get('about', '').strip()}</about>",
        ]
        forbid = self.brand.get("forbidden_phrases", []) or []
        if forbid:
            bits.append("<forbidden-phrases>")
            for p in forbid:
                bits.append(f"  - {p}")
            bits.append("</forbidden-phrases>")
        if self.thesis:
            bits.append(f"<thesis>\n{self.thesis[:6000]}\n</thesis>")
        if self.voice:
            bits.append(f"<voice-rules>\n{self.voice[:4000]}\n</voice-rules>")
        if self.proof_points:
            bits.append(f"<proof-points>\n{self.proof_points[:3000]}\n</proof-points>")
        bits.append("</business-kit>")
        return "\n".join(bits)


def load_kit(slug: str) -> BusinessKit:
    """Load a kit by slug. Raises FileNotFoundError if missing.

    Why not a custom exception: the slug came from a tool arg which
    is user-controlled, so a plain FileNotFoundError with a clear
    message is good enough — the tool wrapper turns it into an MCP
    error response.
    """
    root = _kits_root()
    kit_dir = root / slug
    if not kit_dir.exists() or not kit_dir.is_dir():
        # List what IS available so the error is self-correcting.
        avail = [p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith("_")]
        raise FileNotFoundError(
            f"business-kit '{slug}' not found at {kit_dir}. Available: {avail}"
        )

    brand_path = kit_dir / "brand.yml"
    if not brand_path.exists():
        raise FileNotFoundError(
            f"business-kit '{slug}' is missing brand.yml — "
            f"see {root}/_schema/brand.yml.template"
        )
    brand = yaml.safe_load(brand_path.read_text()) or {}

    def _read(name: str) -> str:
        p = kit_dir / name
        return p.read_text() if p.exists() else ""

    audiences: dict[str, str] = {}
    aud_dir = kit_dir / "audiences"
    if aud_dir.exists():
        for f in aud_dir.glob("*.md"):
            # Persona slug = filename without .md
            audiences[f.stem] = f.read_text()

    return BusinessKit(
        slug=slug,
        brand=brand,
        voice=_read("voice.md"),
        thesis=_read("thesis.md"),
        audiences=audiences,
        proof_points=(kit_dir / "content" / "proof-points.md").read_text()
            if (kit_dir / "content" / "proof-points.md").exists() else "",
        kit_path=kit_dir,
    )


def list_kits() -> list[dict[str, Any]]:
    """List available kits with one-line summary per kit.

    Skips _schema/ and any directory starting with `_`. Returns a
    list of dicts (not BusinessKit objects) because callers usually
    just want a directory listing without paying for full markdown
    reads.
    """
    root = _kits_root()
    if not root.exists():
        return []
    out: list[dict[str, Any]] = []
    for p in sorted(root.iterdir()):
        if not p.is_dir() or p.name.startswith("_"):
            continue
        brand_path = p / "brand.yml"
        if not brand_path.exists():
            continue
        try:
            data = yaml.safe_load(brand_path.read_text()) or {}
        except Exception as e:
            logger.warning("[kits] failed to parse %s: %s", brand_path, e)
            continue
        out.append({
            "slug": data.get("slug", p.name),
            "name": data.get("name", p.name.title()),
            "tagline_short": data.get("tagline_short", ""),
            "audiences": [
                f.stem for f in (p / "audiences").glob("*.md")
            ] if (p / "audiences").exists() else [],
            "kit_path": str(p),
        })
    return out
