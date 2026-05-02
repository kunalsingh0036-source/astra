"""
Unit tests for astra/creators/kits.py — kit loading, listing, prompt rendering.

Uses the test_kit fixture (see tests/fixtures/test_kit/). The fixture
exercises every code path that load_kit() touches: brand.yml parsing,
voice/thesis/proof markdown reads, audience directory enumeration.
"""

from __future__ import annotations

import pytest


class TestLoadKit:
    def test_loads_test_fixture(self, test_kits_dir):
        from astra.creators.kits import load_kit
        from tests.test_creators.conftest import TEST_KIT_SLUG

        kit = load_kit(TEST_KIT_SLUG)
        assert kit.slug == TEST_KIT_SLUG
        assert kit.name == "TestCo"
        assert kit.tagline_short == "Building things that work."

    def test_brand_colors_extracted(self, test_kits_dir):
        from astra.creators.kits import load_kit
        from tests.test_creators.conftest import TEST_KIT_SLUG

        kit = load_kit(TEST_KIT_SLUG)
        colors = kit.colors
        assert colors.get("primary") == "#0F1C2E"
        assert colors.get("secondary") == "#C8B89A"

    def test_fonts_extracted(self, test_kits_dir):
        from astra.creators.kits import load_kit
        from tests.test_creators.conftest import TEST_KIT_SLUG

        kit = load_kit(TEST_KIT_SLUG)
        fonts = kit.fonts
        assert fonts.get("display", {}).get("family") == "Inter"

    def test_voice_thesis_proof_loaded(self, test_kits_dir):
        from astra.creators.kits import load_kit
        from tests.test_creators.conftest import TEST_KIT_SLUG

        kit = load_kit(TEST_KIT_SLUG)
        # Each is a non-empty string
        assert len(kit.voice) > 100
        assert len(kit.thesis) > 100
        assert len(kit.proof_points) > 100
        # Sanity-check content
        assert "Tone in three words" in kit.voice
        assert "TestCo thesis" in kit.thesis
        assert "Customers / clients" in kit.proof_points

    def test_audiences_loaded(self, test_kits_dir):
        from astra.creators.kits import load_kit
        from tests.test_creators.conftest import TEST_KIT_SLUG

        kit = load_kit(TEST_KIT_SLUG)
        assert "test-audience" in kit.audiences
        assert "Test audience" in kit.audiences["test-audience"]

    def test_audience_helper_returns_body(self, test_kits_dir):
        from astra.creators.kits import load_kit
        from tests.test_creators.conftest import TEST_KIT_SLUG

        kit = load_kit(TEST_KIT_SLUG)
        body = kit.audience("test-audience")
        assert "Test audience" in body
        # Missing audience returns empty string, not raises
        assert kit.audience("nonexistent") == ""

    def test_forbidden_phrases_in_brand(self, test_kits_dir):
        from astra.creators.kits import load_kit
        from tests.test_creators.conftest import TEST_KIT_SLUG

        kit = load_kit(TEST_KIT_SLUG)
        forbidden = kit.brand.get("forbidden_phrases") or []
        assert "world-class" in forbidden
        assert "synergy" in forbidden

    def test_unknown_slug_raises(self, test_kits_dir):
        from astra.creators.kits import load_kit

        with pytest.raises(FileNotFoundError) as exc_info:
            load_kit("does-not-exist")
        # Error message must include the available slugs so the caller
        # can self-correct without reading code
        assert "Available" in str(exc_info.value)

    def test_missing_brand_yml_raises(self, tmp_kit_dir):
        """If brand.yml is deleted, load_kit raises with a clear message."""
        from astra.creators.kits import load_kit
        from tests.test_creators.conftest import TEST_KIT_SLUG

        kits_root, kit_dir = tmp_kit_dir
        (kit_dir / "brand.yml").unlink()
        with pytest.raises(FileNotFoundError) as exc:
            load_kit(TEST_KIT_SLUG)
        assert "brand.yml" in str(exc.value)


class TestRenderForPrompt:
    """The kit.render_for_prompt() output is what every draft tool
    feeds to the LLM. Format drift here breaks every tool simultaneously,
    so test the contract carefully."""

    def test_includes_business_kit_marker(self, test_kits_dir):
        from astra.creators.kits import load_kit
        from tests.test_creators.conftest import TEST_KIT_SLUG

        kit = load_kit(TEST_KIT_SLUG)
        prompt = kit.render_for_prompt()
        assert "<business-kit" in prompt
        assert "</business-kit>" in prompt

    def test_includes_tagline(self, test_kits_dir):
        from astra.creators.kits import load_kit
        from tests.test_creators.conftest import TEST_KIT_SLUG

        kit = load_kit(TEST_KIT_SLUG)
        prompt = kit.render_for_prompt()
        assert kit.tagline_short in prompt

    def test_includes_forbidden_phrases(self, test_kits_dir):
        from astra.creators.kits import load_kit
        from tests.test_creators.conftest import TEST_KIT_SLUG

        kit = load_kit(TEST_KIT_SLUG)
        prompt = kit.render_for_prompt()
        # Each forbidden phrase from the fixture appears in the prompt
        for phrase in kit.brand.get("forbidden_phrases") or []:
            assert phrase in prompt

    def test_caps_thesis_and_voice_chars(self, test_kits_dir):
        """The render_for_prompt output truncates thesis/voice/proof to
        avoid blowing the prompt budget. Verify the cap holds even if
        the underlying files grow."""
        from astra.creators.kits import load_kit
        from tests.test_creators.conftest import TEST_KIT_SLUG

        kit = load_kit(TEST_KIT_SLUG)
        # Inflate the thesis and re-check
        kit.thesis = "x" * 20000
        prompt = kit.render_for_prompt()
        # The cap is 6000 chars in the current implementation; verify
        # the output is not the full inflated thesis
        assert "x" * 7000 not in prompt


class TestListKits:
    def test_lists_test_fixture(self, test_kits_dir):
        from astra.creators.kits import list_kits
        from tests.test_creators.conftest import TEST_KIT_SLUG

        kits = list_kits()
        slugs = [k["slug"] for k in kits]
        assert TEST_KIT_SLUG in slugs

    def test_skips_underscore_dirs(self, test_kits_dir):
        """Schema/template directories starting with `_` (like
        `_schema/`) must not appear in list_kits output."""
        from astra.creators.kits import list_kits
        from tests.test_creators.conftest import TEST_KIT_SLUG

        # Add a _schema/ dir to the kits root and assert it's filtered
        schema_dir = test_kits_dir / "_schema"
        schema_dir.mkdir()
        (schema_dir / "brand.yml").write_text("name: schema")
        kits = list_kits()
        slugs = [k["slug"] for k in kits]
        assert "_schema" not in slugs
        assert TEST_KIT_SLUG in slugs

    def test_lists_audiences(self, test_kits_dir):
        from astra.creators.kits import list_kits
        from tests.test_creators.conftest import TEST_KIT_SLUG

        kits = list_kits()
        testco = next(k for k in kits if k["slug"] == TEST_KIT_SLUG)
        assert "test-audience" in testco["audiences"]
