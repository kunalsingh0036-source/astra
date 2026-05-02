"""Tests for astra/creators/draft_carousel.py — social carousel."""
from __future__ import annotations

import json
import pytest

from tests.test_creators.conftest import TEST_KIT_SLUG


VALID_CAROUSEL = json.dumps({
    "title": "TestCo carousel",
    "platform": "linkedin",
    "aspect_ratio": "4:5",
    "hook_promise": "We learned X — here's how",
    "narrative_arc": "Hook → context → insight → cta",
    "slides": [
        {"position": 1, "type": "hook", "headline": "Hook", "body": "Promise.",
         "image_hint": "abstract", "visual_treatment": "minimal"},
        {"position": 2, "type": "context", "headline": "Context", "body": "Setup.",
         "image_hint": "abstract", "visual_treatment": "minimal"},
        {"position": 3, "type": "cta", "headline": "Talk to us", "body": "Email.",
         "image_hint": "logo", "visual_treatment": "branded"},
    ],
    "caption": "Strong opener line. Then context. Then the soft CTA.",
    "first_comment": "Add resources here.",
    "hashtags": ["TestCo", "infrastructure"],
    "best_post_time_hint": "Tuesday morning",
    "engagement_prompt": "What's your take?",
})


@pytest.mark.asyncio
class TestDraftCarouselHappyPath:
    async def test_returns_saved_artifact(self, test_kits_dir, mock_anthropic, mock_store):
        from astra.creators.draft_carousel import draft_carousel
        mock_anthropic(VALID_CAROUSEL)
        artifact = await draft_carousel(
            business_slug=TEST_KIT_SLUG, audience_slug="test-audience",
            topic="testing", platform="linkedin",
        )
        assert artifact["kind"] == "carousel"
        assert artifact["content"]["platform"] == "linkedin"
        assert len(artifact["content"]["slides"]) == 3

    @pytest.mark.parametrize("platform", ["linkedin", "instagram", "twitter"])
    async def test_accepts_all_supported_platforms(
        self, test_kits_dir, mock_anthropic, mock_store, platform,
    ):
        from astra.creators.draft_carousel import draft_carousel
        mock_anthropic(VALID_CAROUSEL)
        artifact = await draft_carousel(
            business_slug=TEST_KIT_SLUG, audience_slug="test-audience",
            topic="x", platform=platform,
        )
        assert artifact["kind"] == "carousel"


@pytest.mark.asyncio
class TestDraftCarouselErrors:
    async def test_unknown_platform_raises(self, test_kits_dir, mock_anthropic, mock_store):
        from astra.creators.draft_carousel import draft_carousel
        mock_anthropic(VALID_CAROUSEL)
        with pytest.raises(ValueError, match="platform"):
            await draft_carousel(
                business_slug=TEST_KIT_SLUG, audience_slug="test-audience",
                topic="x", platform="myspace",
            )

    async def test_unknown_audience_raises(self, test_kits_dir, mock_anthropic, mock_store):
        from astra.creators.draft_carousel import draft_carousel
        mock_anthropic(VALID_CAROUSEL)
        with pytest.raises(FileNotFoundError):
            await draft_carousel(
                business_slug=TEST_KIT_SLUG, audience_slug="missing",
                topic="x", platform="linkedin",
            )
