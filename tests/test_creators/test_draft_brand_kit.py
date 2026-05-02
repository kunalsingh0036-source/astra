"""Tests for astra/creators/draft_brand_kit.py — Top Studios productized service.

Special case: this tool makes FIVE LLM calls (structure JSON + 4 markdown
blobs). The mock_anthropic fixture's `vary` callable handles the multi-call
pattern.
"""
from __future__ import annotations

import json
import pytest

from tests.test_creators.conftest import TEST_KIT_SLUG


# Five responses, one per call:
#  1. structure (JSON)
#  2. voice.md (raw markdown)
#  3. thesis.md
#  4. audience.md
#  5. proof-points.md

STRUCTURE_JSON = json.dumps({
    "slug": "fakeclient",
    "name": "FakeClient",
    "tagline_short": "we make things",
    "tagline_long": "FakeClient makes things, and the things are good.",
    "about": "FakeClient is a fictional client used for testing.",
    "brand": {
        "colors": {
            "primary": "#111111",
            "secondary": "#999999",
            "surface": "#FFFFFF",
            "ink": "#000000",
            "muted": "#666666",
        },
        "typography": {
            "display": {"family": "Inter", "fallback": "system-ui"},
            "body": {"family": "Inter", "fallback": "system-ui"},
        },
        "imagery": "minimalist documentary photography",
    },
    "forbidden_phrases": ["world-class", "best-in-class", "synergy"],
    "primary_audience_slug": "test-buyer",
})

VOICE_MD = "# Voice — FakeClient\n\n## Tone\n\nDirect.\n"
THESIS_MD = "# FakeClient thesis\n\n## In one sentence\n\nThings.\n"
AUDIENCE_MD = "# Audience — Test buyer\n\n## Who they are\n\nA tester.\n"
PROOF_MD = "# Proof points — FakeClient\n\n## Customers\n\n- TBD.\n"


@pytest.fixture
def mock_brand_kit_responses(mock_anthropic):
    """Set up the 5-response sequence the brand-kit drafter expects."""
    responses = iter([STRUCTURE_JSON, VOICE_MD, THESIS_MD, AUDIENCE_MD, PROOF_MD])
    mock_anthropic(lambda **kw: next(responses))
    return None


@pytest.mark.asyncio
class TestDraftBrandKitHappyPath:
    async def test_returns_saved_artifact(
        self, test_kits_dir, mock_brand_kit_responses, mock_store, tmp_path, monkeypatch,
    ):
        from astra.creators.draft_brand_kit import draft_brand_kit

        artifact = await draft_brand_kit(
            client_name="FakeClient",
            audience_hint="test-buyer",
            research_input="Some research about FakeClient.",
            write_to_disk=False,  # skip disk write for clean test isolation
        )
        assert artifact["id"] >= 1
        assert artifact["kind"] == "brand_kit"
        assert artifact["business_slug"] == "top-studios"

    async def test_bundle_includes_all_5_pieces(
        self, test_kits_dir, mock_brand_kit_responses, mock_store,
    ):
        from astra.creators.draft_brand_kit import draft_brand_kit

        artifact = await draft_brand_kit(
            client_name="FakeClient",
            audience_hint="test-buyer",
            research_input="...",
            write_to_disk=False,
        )
        c = artifact["content"]
        assert c["slug"] == "fakeclient"
        assert "Tone" in c["voice_md"]
        assert "thesis" in c["thesis_md"]
        assert c["primary_audience"]["slug"] == "test-buyer"
        assert "Test buyer" in c["primary_audience"]["audience_md"]
        assert "Customers" in c["proof_points_md"]

    async def test_writes_to_disk_creates_loadable_kit(
        self, mock_brand_kit_responses, mock_store, tmp_path, monkeypatch,
    ):
        """When write_to_disk=True, the kit becomes loadable via load_kit."""
        from astra.creators.draft_brand_kit import draft_brand_kit
        from astra.creators.kits import load_kit

        # Point BUSINESS_KITS_DIR at a fresh tmp dir
        monkeypatch.setenv("BUSINESS_KITS_DIR", str(tmp_path / "kits"))
        (tmp_path / "kits").mkdir()

        artifact = await draft_brand_kit(
            client_name="FakeClient",
            audience_hint="test-buyer",
            research_input="...",
            write_to_disk=True,
        )
        assert "kit_path" in artifact

        # Load it back
        kit = load_kit("fakeclient")
        assert kit.name == "FakeClient"
        assert "test-buyer" in kit.audiences


@pytest.mark.asyncio
class TestDraftBrandKitErrors:
    async def test_empty_slug_falls_back_safely(
        self, test_kits_dir, mock_anthropic, mock_store,
    ):
        """When the structure call returns an empty slug, _safe_slug
        falls back to 'client' rather than raising. This is intentional
        defensive behavior — the kit still gets generated and the user
        can review the slug before any disk write happens."""
        from astra.creators.draft_brand_kit import draft_brand_kit

        bad_structure = json.dumps({
            **json.loads(STRUCTURE_JSON),
            "slug": "",  # empty slug — gets coerced to 'client'
        })
        responses = iter([bad_structure, VOICE_MD, THESIS_MD, AUDIENCE_MD, PROOF_MD])
        mock_anthropic(lambda **kw: next(responses))

        artifact = await draft_brand_kit(
            client_name="x", audience_hint="y", research_input="z",
            write_to_disk=False,
        )
        # Falls back to 'client' rather than crashing
        assert artifact["content"]["slug"] == "client"
