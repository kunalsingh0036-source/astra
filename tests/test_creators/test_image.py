"""Tests for astra/creators/image.py — image-prompt + optional Gemini render."""
from __future__ import annotations

import json
import pytest

from tests.test_creators.conftest import TEST_KIT_SLUG


VALID_IMAGE_PROMPT = json.dumps({
    "prompt": "Architectural minimal scene, deep navy and emerald accent, "
              "soft gradient lighting, clean geometric forms",
    "negative_prompt": "stock photos, AI cliches, glowing brain, lens flares",
    "aspect_ratio": "16:9",
    "style_notes": "Architectural clarity, not flashy.",
})


@pytest.mark.asyncio
class TestGenerateHeroImagePromptHappyPath:
    async def test_returns_structured_prompt(self, test_kits_dir, mock_anthropic, mock_store):
        """generate_hero_image_prompt returns the JSON dict directly
        (not a saved artifact — just the prompt)."""
        from astra.creators.image import generate_hero_image_prompt
        mock_anthropic(VALID_IMAGE_PROMPT)
        result = await generate_hero_image_prompt(
            business_slug=TEST_KIT_SLUG,
            image_hint="hero shot for the home page",
            aspect_ratio="16:9",
        )
        assert "prompt" in result
        assert "negative_prompt" in result
        assert result["aspect_ratio"] == "16:9"


@pytest.mark.asyncio
class TestGenerateHeroImageHappyPath:
    async def test_saves_artifact_prompt_only(
        self, test_kits_dir, mock_anthropic, mock_store, monkeypatch,
    ):
        """Without GEMINI_API_KEY, the tool returns prompt-only."""
        from astra.creators.image import generate_hero_image
        # Ensure no Gemini key
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)

        mock_anthropic(VALID_IMAGE_PROMPT)
        artifact = await generate_hero_image(
            business_slug=TEST_KIT_SLUG,
            image_hint="hero shot",
            aspect_ratio="16:9",
        )
        assert artifact["kind"] == "image_prompt"
        # Without key, no rendered image
        assert artifact["content"].get("image_b64") is None
        assert "image_render_status" in artifact["content"]

    async def test_links_to_parent_artifact(
        self, test_kits_dir, mock_anthropic, mock_store, monkeypatch,
    ):
        from astra.creators.image import generate_hero_image
        from astra.creators.store import create_artifact

        monkeypatch.delenv("GEMINI_API_KEY", raising=False)

        # Seed a deck to attach the image to
        deck = await create_artifact(
            business_slug=TEST_KIT_SLUG, kind="deck",
            title="x", ask="x", content={},
        )
        mock_anthropic(VALID_IMAGE_PROMPT)
        artifact = await generate_hero_image(
            business_slug=TEST_KIT_SLUG,
            image_hint="hero",
            parent_artifact_id=deck["id"],
        )
        assert artifact["parent_id"] == deck["id"]


@pytest.mark.asyncio
class TestGenerateHeroImagePromptContent:
    async def test_passes_imagery_anti_patterns_to_model(
        self, test_kits_dir, mock_anthropic, mock_store, monkeypatch,
    ):
        """The kit's imagery direction (with anti-patterns like 'no
        stock photos') must reach the LLM prompt — that's the whole
        point of generating brand-aware prompts."""
        from astra.creators.image import generate_hero_image_prompt
        from unittest.mock import AsyncMock, MagicMock

        captured: dict = {}

        async def capturing_create(**kwargs):
            captured["user"] = kwargs["messages"][0]["content"]
            block = MagicMock(); block.text = VALID_IMAGE_PROMPT
            r = MagicMock(); r.content = [block]
            return r

        fake_client = MagicMock()
        fake_client.messages = MagicMock()
        fake_client.messages.create = AsyncMock(side_effect=capturing_create)
        monkeypatch.setattr("anthropic.AsyncAnthropic", MagicMock(return_value=fake_client))

        await generate_hero_image_prompt(
            business_slug=TEST_KIT_SLUG,
            image_hint="hero shot",
        )
        # The kit's imagery description ("documentary-photography...")
        # must reach the prompt
        assert "documentary" in captured["user"].lower()
